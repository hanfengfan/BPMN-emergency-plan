"""Generate a BPMN 2.0 XML file for the water traffic emergency plan.

This script reads the HTA task decomposition from ``HTA.csv`` and combines it
with contextual information extracted from the textual emergency plan.  The
resulting BPMN model is written to ``water_traffic_emergency.bpmn`` by default
and can be visualised with any BPMN 2.0 compliant tool.

Usage::

    python generate_bpmn.py

Command line options allow overriding the input and output files.  See
``python generate_bpmn.py --help`` for details.
"""

from __future__ import annotations

import argparse
import csv
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET


# Namespace registration so that ElementTree uses the expected prefixes when
# serialising the BPMN model.
ET.register_namespace("bpmn", "http://www.omg.org/spec/BPMN/20100524/MODEL")
ET.register_namespace("bpmndi", "http://www.omg.org/spec/BPMN/20100524/DI")
ET.register_namespace("dc", "http://www.omg.org/spec/DD/20100524/DC")
ET.register_namespace("di", "http://www.omg.org/spec/DD/20100524/DI")


@dataclass
class TaskNode:
    """A node in the HTA task decomposition tree."""

    name: str
    level: int
    description: str = ""
    responsibles: List[str] = field(default_factory=list)
    resources: List[str] = field(default_factory=list)
    parent: Optional["TaskNode"] = None
    children: List["TaskNode"] = field(default_factory=list)
    bpmn_id: Optional[str] = None
    incoming: List[str] = field(default_factory=list)
    outgoing: List[str] = field(default_factory=list)
    plan_context: str = ""

    def add_child(self, child: "TaskNode") -> None:
        if child not in self.children:
            self.children.append(child)

    def update_metadata(
        self,
        description: str,
        responsibles: Iterable[str],
        resources: Iterable[str],
    ) -> None:
        if description and description not in self.description:
            if self.description:
                self.description += "；" + description
            else:
                self.description = description
        for entry in responsibles:
            if entry and entry not in self.responsibles:
                self.responsibles.append(entry)
        for entry in resources:
            if entry and entry not in self.resources:
                self.resources.append(entry)

    @property
    def documentation(self) -> str:
        lines: List[str] = []
        lines.append(f"层级：{self.level}")
        if self.description:
            lines.append(f"任务描述：{self.description}")
        if self.plan_context:
            lines.append(f"预案摘录：{self.plan_context}")
        if self.responsibles:
            lines.append("责任主体：" + "、".join(self.responsibles))
        if self.resources:
            lines.append("所需资源：" + "、".join(self.resources))
        lines.append("任务路径：" + " → ".join(self.path()))
        return "\n".join(lines)

    def path(self) -> List[str]:
        node: Optional[TaskNode] = self
        result: List[str] = []
        while node is not None:
            result.append(node.name)
            node = node.parent
        return list(reversed(result))


def parse_responsibles(raw: str) -> List[str]:
    if not raw:
        return []
    cleaned = raw.replace("/", "、").replace(",", "、").replace("，", "、")
    parts = [part.strip() for part in cleaned.split("、") if part.strip()]
    result: List[str] = []
    for part in parts:
        part = re.sub(r"等$", "", part)
        if part and part not in result:
            result.append(part)
    return result


def parse_resources(raw: str) -> List[str]:
    if not raw:
        return []
    cleaned = raw.replace("/", "、").replace(",", "、").replace("，", "、")
    parts = [part.strip() for part in cleaned.split("、") if part.strip()]
    result: List[str] = []
    for part in parts:
        if part and part not in result:
            result.append(part)
    return result


def parse_hta_csv(path: Path) -> TaskNode:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        nodes_by_level_name: Dict[Tuple[int, str], TaskNode] = {}
        root: Optional[TaskNode] = None

        for row in reader:
            level = int(row["层级编码"].strip())
            name_chain = [part.strip() for part in row["任务名称"].split("→")]
            name = name_chain[-1]
            description = row.get("任务描述", "").strip()
            responsibles = parse_responsibles(row.get("责任主体", "").strip())
            resources = parse_resources(row.get("所需资源", "").strip())

            node_key = (level, name)
            node = nodes_by_level_name.get(node_key)
            if node is None:
                node = TaskNode(name=name, level=level)
                nodes_by_level_name[node_key] = node
            node.update_metadata(description, responsibles, resources)

            if level == 0:
                root = node
                continue

            if len(name_chain) >= 2:
                parent_name = name_chain[-2]
                parent_level = level - 1
            else:
                parent_level = level - 1
                if root is None:
                    raise ValueError("Root node must appear before its children")
                parent_name = root.name

            parent_key = (parent_level, parent_name)
            parent = nodes_by_level_name.get(parent_key)
            if parent is None:
                parent = TaskNode(name=parent_name, level=parent_level)
                nodes_by_level_name[parent_key] = parent
            node.parent = parent
            parent.add_child(node)

        if root is None:
            raise ValueError("HTA CSV does not contain a level 0 root task")

        return root


def iterate_nodes(root: TaskNode) -> Iterable[TaskNode]:
    for child in root.children:
        yield child
        yield from iterate_nodes(child)


def read_plan_paragraphs(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    paragraphs = [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"\n\s*\n", text)
        if part.strip()
    ]
    return paragraphs


def extract_plan_context(paragraphs: Sequence[str], task_name: str) -> str:
    matches = [para for para in paragraphs if task_name in para]
    if not matches and len(task_name) > 2:
        simplified = re.sub(r"[组队小等]", "", task_name)
        matches = [para for para in paragraphs if simplified and simplified in para]
    if not matches:
        return ""
    summary = "；".join(matches[:2])
    summary = summary.replace("\n", " ")
    return textwrap.shorten(summary, width=180, placeholder="…")


def assign_plan_context(root: TaskNode, paragraphs: Sequence[str]) -> None:
    for node in iterate_nodes(root):
        node.plan_context = extract_plan_context(paragraphs, node.name)


def build_lane_map(nodes: Sequence[TaskNode]) -> Dict[str, Dict[str, object]]:
    lane_map: Dict[str, Dict[str, object]] = {}
    for node in nodes:
        responsibles = node.responsibles or ["未指定责任主体"]
        for responsible in responsibles:
            lane = lane_map.get(responsible)
            if lane is None:
                lane = {
                    "id": f"Lane_{len(lane_map) + 1}",
                    "name": responsible,
                    "flow_nodes": [],
                }
                lane_map[responsible] = lane
            lane["flow_nodes"].append(node.bpmn_id)
    return lane_map


def layout_positions(
    ordered_ids: Sequence[str],
    start_id: str,
    end_id: str,
) -> Dict[str, Tuple[float, float, float, float]]:
    y_start = 60.0
    x_position = 200.0
    row_gap = 120.0
    task_height = 80.0
    task_width = 200.0
    event_size = 36.0

    positions: Dict[str, Tuple[float, float, float, float]] = {}
    positions[start_id] = (x_position + 82.0, y_start, event_size, event_size)

    current_y = y_start + row_gap
    for element_id in ordered_ids:
        positions[element_id] = (x_position, current_y, task_width, task_height)
        current_y += row_gap

    positions[end_id] = (x_position + 82.0, current_y, event_size, event_size)
    return positions


def build_bpmn(root: TaskNode, output_path: Path) -> None:
    ordered_nodes = list(iterate_nodes(root))
    for index, node in enumerate(ordered_nodes, start=1):
        node.bpmn_id = f"Activity_{index}"
        node.incoming.clear()
        node.outgoing.clear()

    process_id = "Process_WaterTrafficEmergency"
    start_id = "StartEvent_WaterIncident"
    end_id = "EndEvent_ResponseComplete"

    sequence_flows: List[Tuple[str, str, str]] = []

    previous_id = start_id
    previous_node: Optional[TaskNode] = None
    for node in ordered_nodes:
        flow_id = f"Flow_{len(sequence_flows) + 1}"
        sequence_flows.append((flow_id, previous_id, node.bpmn_id))
        node.incoming.append(flow_id)
        if previous_node is not None:
            previous_node.outgoing.append(flow_id)
        previous_id = node.bpmn_id
        previous_node = node

    final_flow_id = f"Flow_{len(sequence_flows) + 1}"
    sequence_flows.append((final_flow_id, previous_id, end_id))
    if previous_node is not None:
        previous_node.outgoing.append(final_flow_id)

    lane_map = build_lane_map(ordered_nodes)

    definitions = ET.Element(
        "bpmn:definitions",
        attrib={
            "id": "Definitions_WaterTrafficEmergency",
            "targetNamespace": "http://example.com/bpmn/water-traffic-emergency",
        },
    )

    process = ET.SubElement(
        definitions,
        "bpmn:process",
        attrib={
            "id": process_id,
            "name": "水上交通事故应急流程",
            "isExecutable": "false",
        },
    )

    lane_set = ET.SubElement(process, "bpmn:laneSet", attrib={"id": "LaneSet_1"})
    for lane in lane_map.values():
        lane_element = ET.SubElement(
            lane_set,
            "bpmn:lane",
            attrib={"id": lane["id"], "name": lane["name"]},
        )
        for flow_node in lane["flow_nodes"]:
            ET.SubElement(lane_element, "bpmn:flowNodeRef").text = flow_node

    start_event = ET.SubElement(
        process,
        "bpmn:startEvent",
        attrib={"id": start_id, "name": "事故发现/预警"},
    )
    ET.SubElement(start_event, "bpmn:outgoing").text = sequence_flows[0][0]

    for node in ordered_nodes:
        attributes = {"id": node.bpmn_id, "name": node.name}
        task_element = ET.SubElement(process, "bpmn:task", attrib=attributes)
        for incoming in node.incoming:
            ET.SubElement(task_element, "bpmn:incoming").text = incoming
        for outgoing in node.outgoing:
            ET.SubElement(task_element, "bpmn:outgoing").text = outgoing
        documentation = ET.SubElement(task_element, "bpmn:documentation")
        documentation.text = node.documentation
    end_event = ET.SubElement(
        process,
        "bpmn:endEvent",
        attrib={"id": end_id, "name": "响应结束"},
    )
    ET.SubElement(end_event, "bpmn:incoming").text = sequence_flows[-1][0]
    for flow_id, source_id, target_id in sequence_flows:
        ET.SubElement(
            process,
            "bpmn:sequenceFlow",
            attrib={"id": flow_id, "sourceRef": source_id, "targetRef": target_id},
        )
    ordered_ids = [node.bpmn_id for node in ordered_nodes]
    positions = layout_positions(ordered_ids, start_id, end_id)
    diagram = ET.SubElement(
        definitions,
        "bpmndi:BPMNDiagram",
        attrib={"id": "BPMNDiagram_WaterTrafficEmergency"},
    )
    plane = ET.SubElement(
        diagram,
        "bpmndi:BPMNPlane",
        attrib={"id": "BPMNPlane_WaterTrafficEmergency", "bpmnElement": process_id},
    )
    for element_id, (x, y, width, height) in positions.items():
        shape = ET.SubElement(
            plane,
            "bpmndi:BPMNShape",
            attrib={"id": f"{element_id}_di", "bpmnElement": element_id},
        )
        ET.SubElement(
            shape,
            "dc:Bounds",
            attrib={
                "x": f"{x:.2f}",
                "y": f"{y:.2f}",
                "width": f"{width:.2f}",
                "height": f"{height:.2f}",
            },
        )
    def element_bottom(bounds: Tuple[float, float, float, float]) -> Tuple[float, float]:
        x, y, width, height = bounds
        return (x + width / 2.0, y + height)
    def element_top(bounds: Tuple[float, float, float, float]) -> Tuple[float, float]:
        x, y, width, _ = bounds
        return (x + width / 2.0, y)
    for flow_id, source_id, target_id in sequence_flows:
        edge = ET.SubElement(
            plane,
            "bpmndi:BPMNEdge",
            attrib={"id": f"{flow_id}_di", "bpmnElement": flow_id},
        )
        source_bounds = positions[source_id]
        target_bounds = positions[target_id]
        if source_id == start_id:
            start_point = element_bottom(source_bounds)
        else:
            start_point = element_bottom(source_bounds)
        if target_id == end_id:
            end_point = element_top(target_bounds)
        else:
            end_point = element_top(target_bounds)
        for point in (start_point, end_point):
            ET.SubElement(
                edge,
                "di:waypoint",
                attrib={"x": f"{point[0]:.2f}", "y": f"{point[1]:.2f}"},
            )
    tree = ET.ElementTree(definitions)
    ET.indent(tree, space="  ", level=0)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


def build_and_export(
    hta_path: Path,
    plan_path: Path,
    output_path: Path,
) -> None:
    root = parse_hta_csv(hta_path)
    paragraphs = read_plan_paragraphs(plan_path)
    assign_plan_context(root, paragraphs)
    build_bpmn(root, output_path)


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hta",
        type=Path,
        default=Path("HTA.csv"),
        help="Path to the HTA CSV file",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("天津港大港港区10万吨级航道提升工程施工（二标段）专项应急预案 - 水上交通事故.txt"),
        help="Path to the emergency plan text file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("water_traffic_emergency.bpmn"),
        help="Path to the generated BPMN XML file",
    )
    args = parser.parse_args(argv)
    build_and_export(args.hta, args.plan, args.output)


if __name__ == "__main__":
    main()
