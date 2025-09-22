from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from xml.dom import minidom


@dataclass
class HTATask:
    name: str
    description: str = ""
    owners: List[str] = field(default_factory=list)
    resources: List[str] = field(default_factory=list)
    level: int = 0
    children: List["HTATask"] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "owners": self.owners,
            "resources": self.resources,
            "level": self.level,
            "children": [child.to_dict() for child in self.children],
        }


def split_values(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [re.sub(r"等$", "", part.strip()) for part in re.split(r"[\/、，,；;]", raw) if part.strip()]
    cleaned: List[str] = []
    for part in parts:
        if part and part not in cleaned:
            cleaned.append(part)
    return cleaned


def build_hta_tree(csv_path: Path) -> HTATask:
    node_lookup: Dict[Tuple[str, ...], HTATask] = {}
    name_index: Dict[str, List[Tuple[str, ...]]] = defaultdict(list)
    root: HTATask | None = None
    root_path: Tuple[str, ...] | None = None

    def ensure_node(path: Tuple[str, ...]) -> HTATask:
        nonlocal root
        if path in node_lookup:
            return node_lookup[path]
        node = HTATask(name=path[-1], level=len(path) - 1)
        node_lookup[path] = node
        name_index[node.name].append(path)
        if len(path) == 1:
            if root is None:
                root = node
        else:
            parent = ensure_node(path[:-1])
            if node not in parent.children:
                parent.children.append(node)
        return node

    with csv_path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            level = int(row["层级编码"].strip())
            segments = [segment.strip() for segment in row["任务名称"].split("→") if segment.strip()]
            if not segments:
                continue
            name = segments[-1]
            if level == 0:
                path = (name,)
                root_path = path
            elif level == 1:
                if root_path is None:
                    raise ValueError("在读取一级任务前未定义根节点")
                path = (*root_path, name)
            else:
                parent_name = segments[-2] if len(segments) > 1 else None
                if parent_name is None:
                    raise ValueError(f"无法确定任务 '{name}' 的父节点")
                parent_paths = name_index.get(parent_name)
                if not parent_paths:
                    raise ValueError(f"未找到父节点 '{parent_name}' 对应的路径")
                parent_path = parent_paths[-1]
                path = (*parent_path, name)
            node = ensure_node(path)
            node.level = level
            node.description = row["任务描述"].strip()
            node.owners = split_values(row["责任主体"])
            node.resources = split_values(row["所需资源"])

    if root is None:
        raise ValueError("HTA树未能构建，请检查CSV内容")
    return root


def parse_applicability(text: str) -> str:
    match = re.search(r"####\s*2\.2\.1\s+适用范围\r?\n(.*?)(?=\r?\n####[^#]|\Z)", text, re.S)
    if not match:
        return ""
    content = " ".join(line.strip() for line in match.group(1).splitlines() if line.strip())
    return content


def parse_ordered_lines(lines: Iterable[str]) -> List[str]:
    items: List[str] = []
    current: List[str] = []
    pattern = re.compile(r"^(\(\d+\)|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫])")
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if pattern.match(line):
            if current:
                items.append(" ".join(current).strip())
            current = [line]
        else:
            if current:
                current.append(line)
            else:
                current = [line]
    if current:
        items.append(" ".join(current).strip())
    return items


def parse_risk_description(text: str) -> Dict[str, List[str]]:
    match = re.search(r"####\s*2\.2\.2\s+风险事件描述\r?\n(.*?)(?=\r?\n####[^#]|\Z)", text, re.S)
    if not match:
        return {}
    section = match.group(1)
    result: Dict[str, List[str]] = {}
    current_key: str | None = None
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        num_match = re.match(r"(\d+)、(.+)", line)
        if num_match:
            current_key = num_match.group(2).strip()
            result[current_key] = []
        elif current_key is not None:
            result[current_key].append(line)
    for key, values in result.items():
        result[key] = parse_ordered_lines(values)
    return result


def parse_procedure(text: str) -> List[str]:
    match = re.search(r"####\s*2\.2\.4\s+处置程序\r?\n(.*?)(?=\r?\n####[^#]|\Z)", text, re.S)
    if not match:
        return []
    section = match.group(1)
    lines = section.splitlines()
    return parse_ordered_lines(lines)


def parse_measures(text: str) -> Dict[str, List[str]]:
    match = re.search(r"####\s*2\.2\.5\s+应急处置措施\r?\n(.*?)(?=\r?\n####[^#]|\Z)", text, re.S)
    if not match:
        return {}
    section = match.group(1)
    measures: Dict[str, List[str]] = {}
    # 前置说明
    prefix = section.split("#####", 1)[0].strip()
    if prefix:
        measures["总体要求"] = [line.strip() for line in prefix.splitlines() if line.strip()]

    pattern = re.compile(r"#####\s*2\.2\.5\.(\d+)\s+(.+?)\r?\n(.*?)(?=\r?\n#####|\Z)", re.S)
    for match in pattern.finditer(section):
        index = match.group(1)
        title = match.group(2).strip()
        content = match.group(3)
        key = f"{index} {title}"
        measures[key] = parse_ordered_lines(content.splitlines())
    return measures


def extract_plan_elements(plan_path: Path) -> Dict[str, object]:
    text = plan_path.read_text(encoding="utf-8")
    return {
        "applicability": parse_applicability(text),
        "risk_description": parse_risk_description(text),
        "procedure": parse_procedure(text),
        "measures": parse_measures(text),
    }


def flatten_tasks(node: HTATask, path: List[str] | None = None, parent: HTATask | None = None,
                  preceding: str = "无", following: str = "无") -> List[Dict]:
    if path is None:
        path = []
    current_path = path + [node.name]
    entry = {
        "层级": node.level,
        "任务名称": node.name,
        "任务路径": " > ".join(current_path),
        "任务描述": node.description,
        "责任主体": node.owners,
        "所需资源": node.resources,
        "父级任务": parent.name if parent else "无",
        "前置任务": preceding,
        "后置任务": following,
    }
    records = [entry]
    for idx, child in enumerate(node.children):
        child_preceding = node.name if idx == 0 else node.children[idx - 1].name
        child_following = node.children[idx + 1].name if idx < len(node.children) - 1 else node.name
        records.extend(flatten_tasks(child, current_path, node, child_preceding, child_following))
    return records


def summarise_subtasks(task: HTATask) -> List[Dict[str, object]]:
    summary: List[Dict[str, object]] = []
    for child in task.children:
        item = {
            "name": child.name,
            "description": child.description,
            "owners": child.owners,
        }
        if child.children:
            item["subtasks"] = [grandchild.name for grandchild in child.children]
        summary.append(item)
    return summary


def generate_bpmn_model(root: HTATask, task_records: List[Dict]) -> Dict[str, object]:
    top_levels = {child.name: child for child in root.children}
    response = top_levels.get("应急响应")
    onsite = top_levels.get("现场处置")
    recovery = top_levels.get("后期恢复")
    pre_warning = top_levels.get("事故预警")
    report = top_levels.get("事故报告")

    nodes = [
        {"id": "start", "name": "事故发现/预警", "type": "start_event"},
    ]
    if pre_warning:
        nodes.append({
            "id": "pre_warning",
            "name": pre_warning.name,
            "type": "task",
            "lane": "/".join(pre_warning.owners) if pre_warning.owners else "项目部应急领导小组",
            "details": summarise_subtasks(pre_warning),
        })
    if report:
        nodes.append({
            "id": "report",
            "name": report.name,
            "type": "task",
            "lane": "/".join(report.owners) if report.owners else "信息联络组",
            "details": summarise_subtasks(report),
        })
    if response:
        nodes.append({
            "id": "response",
            "name": response.name,
            "type": "sub_process",
            "lane": "/".join(response.owners) if response.owners else "项目部应急领导小组",
            "details": summarise_subtasks(response),
        })
    nodes.extend([
        {"id": "gateway_1", "name": "处置能力判断", "type": "exclusive_gateway"},
        {"id": "external_support", "name": "社会力量支援协调", "type": "task", "lane": "政府及外部机构"},
    ])
    if onsite:
        nodes.append({
            "id": "onsite",
            "name": onsite.name,
            "type": "sub_process",
            "lane": "/".join(onsite.owners) if onsite.owners else "现场处置小组",
            "details": summarise_subtasks(onsite),
        })
    if recovery:
        nodes.append({
            "id": "recovery",
            "name": recovery.name,
            "type": "task",
            "lane": "/".join(recovery.owners) if recovery.owners else "善后处置组",
            "details": summarise_subtasks(recovery),
        })
    nodes.append({"id": "end", "name": "响应结束/进入善后", "type": "end_event"})

    edges = [
        {"from": "start", "to": "pre_warning" if pre_warning else "report"},
    ]
    if pre_warning and report:
        edges.append({"from": "pre_warning", "to": "report"})
    if report and response:
        edges.append({"from": "report", "to": "response"})
    elif report:
        edges.append({"from": "report", "to": "gateway_1"})
    if response:
        edges.append({"from": "response", "to": "gateway_1"})
    edges.extend([
        {"from": "gateway_1", "to": "onsite", "condition": "项目处置能力充足"},
        {"from": "gateway_1", "to": "external_support", "condition": "需外部增援"},
        {"from": "external_support", "to": "onsite"},
    ])
    if onsite:
        edges.append({"from": "onsite", "to": "recovery" if recovery else "end"})
    if recovery:
        edges.append({"from": "recovery", "to": "end"})
    else:
        edges.append({"from": "onsite", "to": "end"})

    lane_map: Dict[str, List[str]] = defaultdict(list)
    for record in task_records:
        for owner in record["责任主体"]:
            if record["任务名称"] not in lane_map[owner]:
                lane_map[owner].append(record["任务名称"])
    lane_map.setdefault("政府及外部机构", []).append("社会力量支援协调")

    message_flows = [
        {"from": "现场处置小组", "to": "项目部应急领导小组", "message": "险情信息与现场动态"},
        {"from": "项目部应急领导小组", "to": "交通/应急/海事主管部门", "message": "事故报告与救援请求"},
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "lanes": lane_map,
        "message_flows": message_flows,
    }


def build_bpmn_xml(bpmn_model: Dict[str, object]) -> str:
    bpmn_ns = "http://www.omg.org/spec/BPMN/20100524/MODEL"
    bpmndi_ns = "http://www.omg.org/spec/BPMN/20100524/DI"
    dc_ns = "http://www.omg.org/spec/DD/20100524/DC"
    di_ns = "http://www.omg.org/spec/DD/20100524/DI"

    ET.register_namespace("", bpmn_ns)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    ET.register_namespace("bpmndi", bpmndi_ns)
    ET.register_namespace("dc", dc_ns)
    ET.register_namespace("di", di_ns)

    def qname(name: str) -> str:
        return f"{{{bpmn_ns}}}{name}"

    definitions = ET.Element(
        qname("definitions"),
        {
            "id": "Definitions_WaterwayEmergency",
            "targetNamespace": "http://example.com/bpmn/emergency-plan",
        },
    )

    process = ET.SubElement(
        definitions,
        qname("process"),
        {
            "id": "Process_WaterwayEmergency",
            "name": "水上交通事故应急处置流程",
            "isExecutable": "false",
        },
    )

    nodes: List[Dict[str, object]] = list(bpmn_model.get("nodes", []))  # type: ignore
    lane_assignments: Dict[str, List[str]] = defaultdict(list)
    for node in nodes:
        node_id = str(node["id"])  # type: ignore[index]
        lane_name = str(node.get("lane") or "流程控制")
        lane_assignments[lane_name].append(node_id)

    lane_infos = []
    lane_set = ET.SubElement(process, qname("laneSet"), {"id": "LaneSet_WaterwayEmergency"})
    for idx, (lane_name, node_ids) in enumerate(sorted(lane_assignments.items()), start=1):
        lane_id = f"Lane_{idx}"
        lane = ET.SubElement(
            lane_set,
            qname("lane"),
            {
                "id": lane_id,
                "name": lane_name,
            },
        )
        for node_id in node_ids:
            ET.SubElement(lane, qname("flowNodeRef")).text = node_id
        lane_infos.append({"name": lane_name, "id": lane_id, "node_ids": node_ids})

    type_map = {
        "start_event": "startEvent",
        "end_event": "endEvent",
        "task": "task",
        "sub_process": "subProcess",
        "exclusive_gateway": "exclusiveGateway",
    }

    lane_index_map = {info["name"]: pos for pos, info in enumerate(lane_infos)}
    lane_height = 160
    lane_gap = 20
    base_y = 120
    base_x = 140
    x_spacing = 220

    def node_dimensions(node_type: str) -> Tuple[int, int]:
        if node_type == "start_event" or node_type == "end_event":
            return 36, 36
        if node_type == "exclusive_gateway":
            return 50, 50
        if node_type == "sub_process":
            return 170, 110
        return 140, 90

    node_positions: Dict[str, Dict[str, float]] = {}

    for idx, node in enumerate(nodes):
        node_id = str(node["id"])  # type: ignore[index]
        node_type = str(node.get("type", "task"))
        tag_name = type_map.get(node_type, "task")
        attributes = {"id": node_id, "name": str(node.get("name", ""))}
        if node_type == "exclusive_gateway":
            attributes["gatewayDirection"] = "Diverging"
        element = ET.SubElement(process, qname(tag_name), attributes)
        details = node.get("details")
        documentation_lines: List[str] = []
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                name = str(detail.get("name", ""))
                description = str(detail.get("description", ""))
                owners = detail.get("owners")
                subtasks = detail.get("subtasks")
                pieces = [name]
                if description:
                    pieces.append(description)
                if owners:
                    pieces.append("责任：" + "、".join(str(owner) for owner in owners))
                if subtasks:
                    pieces.append("下级任务：" + "、".join(str(item) for item in subtasks))
                documentation_lines.append("；".join(piece for piece in pieces if piece))
        if documentation_lines:
            ET.SubElement(element, qname("documentation")).text = "\n".join(documentation_lines)

        lane_name = str(node.get("lane") or "流程控制")
        lane_index = lane_index_map.get(lane_name, 0)
        width, height = node_dimensions(node_type)
        lane_y = base_y + lane_index * (lane_height + lane_gap)
        node_y = lane_y + (lane_height - height) / 2
        node_x = base_x + idx * x_spacing
        node_positions[node_id] = {"x": node_x, "y": node_y, "width": width, "height": height}

    sequence_flows: List[Tuple[str, str, str]] = []
    for idx, edge in enumerate(bpmn_model.get("edges", [])):  # type: ignore[call-arg]
        if not isinstance(edge, dict):
            continue
        attributes = {
            "id": f"Flow_{idx + 1}",
            "sourceRef": str(edge.get("from", "")),
            "targetRef": str(edge.get("to", "")),
        }
        condition = edge.get("condition")
        if condition:
            attributes["name"] = str(condition)
        ET.SubElement(process, qname("sequenceFlow"), attributes)
        sequence_flows.append((attributes["id"], attributes["sourceRef"], attributes["targetRef"]))

    message_flows = bpmn_model.get("message_flows", [])
    documentation_lines = ["自动生成的 BPMN 2.0 流程模型。"]
    if isinstance(message_flows, list) and message_flows:
        documentation_lines.append("消息流：")
        for flow in message_flows:
            if not isinstance(flow, dict):
                continue
            documentation_lines.append(
                f"{flow.get('from', '')} -> {flow.get('to', '')}：{flow.get('message', '')}"
            )
    ET.SubElement(process, qname("documentation")).text = "\n".join(documentation_lines)

    diagram = ET.SubElement(
        definitions,
        f"{{{bpmndi_ns}}}BPMNDiagram",
        {"id": "BPMNDiagram_WaterwayEmergency"},
    )
    plane = ET.SubElement(
        diagram,
        f"{{{bpmndi_ns}}}BPMNPlane",
        {"id": "BPMNPlane_WaterwayEmergency", "bpmnElement": "Process_WaterwayEmergency"},
    )

    max_x = base_x + max(len(nodes) - 1, 0) * x_spacing + 220

    for lane_idx, info in enumerate(lane_infos):
        lane_y = base_y + lane_idx * (lane_height + lane_gap) - 10
        lane_shape = ET.SubElement(
            plane,
            f"{{{bpmndi_ns}}}BPMNShape",
            {"id": f"{info['id']}_di", "bpmnElement": info["id"]},
        )
        ET.SubElement(
            lane_shape,
            f"{{{dc_ns}}}Bounds",
            {
                "x": f"{base_x - 120:.1f}",
                "y": f"{lane_y:.1f}",
                "width": f"{max_x - (base_x - 120):.1f}",
                "height": f"{lane_height + 20:.1f}",
            },
        )

    for node_id, position in node_positions.items():
        shape = ET.SubElement(
            plane,
            f"{{{bpmndi_ns}}}BPMNShape",
            {"id": f"{node_id}_di", "bpmnElement": node_id},
        )
        ET.SubElement(
            shape,
            f"{{{dc_ns}}}Bounds",
            {
                "x": f"{position['x']:.1f}",
                "y": f"{position['y']:.1f}",
                "width": f"{position['width']:.1f}",
                "height": f"{position['height']:.1f}",
            },
        )

    for flow_id, source_id, target_id in sequence_flows:
        edge = ET.SubElement(
            plane,
            f"{{{bpmndi_ns}}}BPMNEdge",
            {"id": f"{flow_id}_di", "bpmnElement": flow_id},
        )
        source = node_positions.get(source_id)
        target = node_positions.get(target_id)
        if source and target:
            start_x = source["x"] + source["width"]
            start_y = source["y"] + source["height"] / 2
            end_x = target["x"]
            end_y = target["y"] + target["height"] / 2
            ET.SubElement(
                edge,
                f"{{{di_ns}}}waypoint",
                {"x": f"{start_x:.1f}", "y": f"{start_y:.1f}"},
            )
            ET.SubElement(
                edge,
                f"{{{di_ns}}}waypoint",
                {"x": f"{end_x:.1f}", "y": f"{end_y:.1f}"},
            )

    rough_xml = ET.tostring(definitions, encoding="utf-8")
    parsed = minidom.parseString(rough_xml)
    pretty = parsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    return pretty


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_task_database(path: Path, records: List[Dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["层级", "任务路径", "任务名称", "任务描述", "责任主体", "所需资源", "父级任务", "前置任务", "后置任务"])
        for record in records:
            writer.writerow([
                record["层级"],
                record["任务路径"],
                record["任务名称"],
                record["任务描述"],
                "；".join(record["责任主体"]) if record["责任主体"] else "无",
                "；".join(record["所需资源"]) if record["所需资源"] else "无",
                record["父级任务"],
                record["前置任务"],
                record["后置任务"],
            ])


def count_descendants(task: HTATask) -> int:
    total = len(task.children)
    for child in task.children:
        total += count_descendants(child)
    return total


def render_tree_lines(task: HTATask, depth: int = 0, max_depth: int = 2) -> List[str]:
    indent = "  " * depth
    description = f"：{task.description}" if task.description else ""
    lines = [f"{indent}- {task.name}{description}"]
    if depth >= max_depth:
        return lines
    for child in task.children:
        lines.extend(render_tree_lines(child, depth + 1, max_depth))
    return lines


def generate_report(output_path: Path, elements: Dict[str, object], root: HTATask,
                    task_records: List[Dict], bpmn_model: Dict[str, object],
                    bpmn_xml: str) -> None:
    counts = Counter(record["层级"] for record in task_records)
    responsibilities = sorted({owner for record in task_records for owner in record["责任主体"]})
    resources = sorted({res for record in task_records for res in record["所需资源"]})

    top_level_info = []
    for child in root.children:
        top_level_info.append({
            "name": child.name,
            "description": child.description,
            "level": child.level,
            "descendants": count_descendants(child),
        })

    tree_lines = render_tree_lines(root, max_depth=2)
    full_tree_lines = render_tree_lines(root, max_depth=10)
    procedure_steps: List[str] = elements.get("procedure", [])  # type: ignore
    procedure_lines = [f"{idx + 1}. {step}" for idx, step in enumerate(procedure_steps)]

    measures_section = []
    measures: Dict[str, List[str]] = elements.get("measures", {})  # type: ignore
    for name, steps in measures.items():
        preview = "；".join(steps[:2]) + ("……" if len(steps) > 2 else "") if steps else ""
        measures_section.append(f"- **{name}**：{preview}")

    lanes = bpmn_model.get("lanes", {})
    lane_lines = []
    for lane_name, tasks in sorted(lanes.items(), key=lambda item: (-len(item[1]), item[0]))[:6]:
        lane_lines.append(f"- {lane_name}（涉及{len(tasks)}个任务）")

    sample_entries: List[Dict] = []
    top_names = [child.name for child in root.children]
    for top_name in top_names:
        prefix = f"{root.name} > {top_name}"
        for record in task_records:
            if record["层级"] >= 2 and record["任务路径"].startswith(prefix):
                sample_entries.append(record)
                break
    if len(sample_entries) < 5:
        for record in task_records:
            if record["层级"] >= 2 and record not in sample_entries:
                sample_entries.append(record)
            if len(sample_entries) >= 5:
                break

    task_table_lines = [
        "| 层级 | 任务路径 | 任务描述 | 责任主体 | 所需资源 | 父级任务 | 前置任务 | 后置任务 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in task_records:
        task_table_lines.append(
            "| {level} | {path} | {desc} | {owners} | {resources} | {parent} | {preceding} | {following} |".format(
                level=record["层级"],
                path=record["任务路径"],
                desc=record["任务描述"] or "-",
                owners="、".join(record["责任主体"]) or "无",
                resources="、".join(record["所需资源"]) or "无",
                parent=record["父级任务"],
                preceding=record["前置任务"],
                following=record["后置任务"],
            )
        )

    bpmn_xml_lines = [line for line in bpmn_xml.strip().splitlines()] if bpmn_xml.strip() else []

    report_lines = [
        "# 水上交通事故专项预案数字化应用与效果",
        "",
        "## 1. 案例基础信息",
        f"- **适用范围**：{elements.get('applicability', '')}",
        "- **风险事件描述**：",
    ]

    risk_description: Dict[str, List[str]] = elements.get("risk_description", {})  # type: ignore
    for key, items in risk_description.items():
        report_lines.append(f"  - {key}：")
        if not items:
            continue
        for item in items:
            report_lines.append(f"    - {item}")

    report_lines.extend([
        "",
        "## 2. 预案要素解析结果",
        "- **处置程序关键步骤**：",
    ])
    report_lines.extend([f"  - {line}" for line in procedure_lines] or ["  - 未在原文中检索到明确步骤" ])
    if measures_section:
        report_lines.append("- **专项处置要点概览**：")
        report_lines.extend([f"  {line}" for line in measures_section])

    report_lines.extend([
        "",
        "## 3. HTA 任务分解树成果",
        f"- 共识别任务 {len(task_records)} 个，其中各层级分布：" + "，".join(
            f"第{level}层 {count} 个" for level, count in sorted(counts.items())
        ),
        "- 顶层任务摘要：",
    ])
    for info in top_level_info:
        report_lines.append(
            f"  - {info['name']}（说明：{info['description']}，包含下级任务 {info['descendants']} 个）"
        )
    report_lines.append("- 任务树结构片段：")
    report_lines.extend([f"  {line}" for line in tree_lines])

    report_lines.extend([
        "",
        "## 4. BPMN 流程建模结果",
        f"- 生成流程节点 {len(bpmn_model.get('nodes', []))} 个，顺序流 {len(bpmn_model.get('edges', []))} 条。",
        "- 泳道分配摘要：",
    ])
    report_lines.extend(lane_lines or ["- 未识别到责任主体信息" ])
    report_lines.append("- 典型消息流：")
    for flow in bpmn_model.get("message_flows", []):
        report_lines.append(f"  - {flow['from']} → {flow['to']}：{flow['message']}")

    report_lines.extend([
        "",
        "## 5. 任务-责任-资源数据库概览",
        f"- 涉及责任主体 {len(responsibilities)} 个：" + "、".join(responsibilities),
        f"- 资源条目 {len(resources)} 种：" + "、".join(resources[:10]) + (" 等" if len(resources) > 10 else ""),
        "- 数据库样例：",
        "",
        "| 任务路径 | 责任主体 | 所需资源 |",
        "| --- | --- | --- |",
    ])
    for record in sample_entries:
        report_lines.append(
            f"| {record['任务路径']} | {'、'.join(record['责任主体']) or '无'} | "
            f"{'、'.join(record['所需资源']) or '无'} |"
        )

    report_lines.extend([
        "",
        "## 6. 应用效果与价值",
        "- 文本预案被结构化为可查询的任务树与流程模型，便于应急演练与责任落实。",
        "- BPMN 流程明确项目级与社会级响应的衔接，使跨部门协作路径可视化。",
        "- 数据库化管理实现任务、责任、资源的快速索引，可支撑演练脚本编排和资源调度。",
    ])

    full_applicability = elements.get("applicability", "") or "原文未提及"

    report_lines.extend([
        "",
        "## 7. 全量结果汇编",
        "",
        "### 7.1 预案要素完整呈现",
        "",
        "#### 适用范围",
        full_applicability,
        "",
        "#### 风险事件描述",
    ])
    if risk_description:
        for key, items in risk_description.items():
            report_lines.append(f"- {key}")
            if items:
                for item in items:
                    report_lines.append(f"  - {item}")
            else:
                report_lines.append("  - （原文未列出具体描述）")
    else:
        report_lines.append("- 原文未检索到详细描述")

    report_lines.extend([
        "",
        "#### 处置程序",
    ])
    if procedure_steps:
        for idx, step in enumerate(procedure_steps, start=1):
            report_lines.append(f"{idx}. {step}")
    else:
        report_lines.append("无明确步骤")

    report_lines.extend([
        "",
        "#### 应急处置措施",
    ])
    if measures:
        for name, steps in measures.items():
            report_lines.append(f"- {name}")
            if steps:
                for item in steps:
                    report_lines.append(f"  - {item}")
            else:
                report_lines.append("  - （原文未列出细化步骤）")
    else:
        report_lines.append("- 原文未检索到具体措施")

    report_lines.extend([
        "",
        "### 7.2 HTA 任务树全景",
        "完整任务结构如下：",
    ])
    report_lines.extend(full_tree_lines)

    report_lines.extend([
        "",
        "### 7.3 BPMN 2.0 标准模型",
        "流程 BPMN 2.0 XML 文件已输出至 `output/bpmn_model.bpmn`，完整内容如下：",
        "",
        "```xml",
    ])
    report_lines.extend(bpmn_xml_lines or ["<!-- 无可用流程模型 -->"])
    report_lines.append("```")

    report_lines.extend([
        "",
        "### 7.4 任务-责任-资源全量表",
        "下表列出全部任务条目及其责任主体与资源配置：",
    ])
    report_lines.extend(task_table_lines)

    output_path.write_text("\n".join(report_lines), encoding="utf-8")


def main() -> None:
    base_dir = Path(__file__).parent
    output_dir = base_dir / "output"
    output_dir.mkdir(exist_ok=True)

    hta_root = build_hta_tree(base_dir / "HTA.csv")
    plan_elements = extract_plan_elements(base_dir / "天津港大港港区10万吨级航道提升工程施工（二标段）专项应急预案 - 水上交通事故.txt")

    task_records = flatten_tasks(hta_root)
    bpmn_model = generate_bpmn_model(hta_root, task_records)
    bpmn_xml = build_bpmn_xml(bpmn_model)

    write_json(output_dir / "hta_tree.json", hta_root.to_dict())
    write_json(output_dir / "plan_elements.json", plan_elements)
    write_task_database(output_dir / "task_responsibility_resource.csv", task_records)
    write_json(output_dir / "bpmn_model.json", bpmn_model)
    (output_dir / "bpmn_model.bpmn").write_text(bpmn_xml, encoding="utf-8")
    generate_report(
        output_dir / "application_report.md",
        plan_elements,
        hta_root,
        task_records,
        bpmn_model,
        bpmn_xml,
    )

    print("已生成 HTA 树、BPMN 模型、任务数据库及应用效果报告，位于 output/ 目录内。")


if __name__ == "__main__":
    main()
