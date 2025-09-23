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
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
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


@dataclass(frozen=True)
class TaskEnrichment:
    """Additional details extracted from the emergency plan."""

    name: Optional[str] = None
    description: Optional[str] = None
    plan_context: Optional[str] = None


TASK_ENRICHMENTS: Dict[str, TaskEnrichment] = {
    "总体应急目标 → 事故预警": TaskEnrichment(
        name="监测施工水域风险并预警",
        description="综合协调组联合技术支持组围绕船员、船舶与环境因素评估碰撞、火灾、风浪、自沉等风险，提前调整施工计划并准备应对措施。",
        plan_context="预案2.2.2指出水上作业可能因船员因素、船舶因素和环境因素引发碰撞、火灾、风灾、自沉、搁浅等事故，需要提前识别风险并采取预警措施。",
    ),
    "总体应急目标 → 事故预警 → 风险监测": TaskEnrichment(
        name="巡查施工与航道风险点",
        description="技术支持组对施工海域的船舶运行、作业干扰和周边航行环境实施巡查，跟踪可能诱发水上交通事故的风险点。",
        plan_context="预案2.2.2(1)-(7)列举了船舶碰撞、火灾爆炸、风浪损伤、自沉、搁浅及人员落水等风险情形，要求持续监测施工与航道状况。",
    ),
    "总体应急目标 → 事故预警 → 风险监测 → 日常巡查": TaskEnrichment(
        name="检查船舶与设备状态",
        description="落实船舶机舱、泵舱、消防、通信等设备的例行检查，发现设备异常及时调整，避免因设备缺陷诱发事故。",
        plan_context="预案2.2.2强调船舶因素可能引发火灾、爆炸、自沉等险情，需要通过日常巡查掌握设备运行状态。",
    ),
    "总体应急目标 → 事故预警 → 风险监测 → 环境监测": TaskEnrichment(
        name="掌握气象与水文条件",
        description="跟踪风力、突风、潮差和能见度等环境条件，及时采取避险或调整航行安排，降低风灾和浪损风险。",
        plan_context="预案2.2.2(4)提示海上施工易受大风、突风影响发生风灾、浪损事故，需要加强环境监测。",
    ),
    "总体应急目标 → 事故预警 → 预警发布": TaskEnrichment(
        name="发布预警等级与调整施工",
        description="项目部应急办公室依据风险巡查结果划分蓝、黄、橙、红预警等级，通知相关单位调整作业强度并做好防范部署。",
        plan_context="预案要求根据监测到的碰撞、火灾、自沉等风险提前发布预警信息，引导施工单位采取防范措施。",
    ),
    "总体应急目标 → 事故预警 → 预警发布 → 信息通报": TaskEnrichment(
        name="通知各小组执行预警措施",
        description="综合协调组向各应急小组通报预警等级，明确值守、避风、减载、调整航次等协同动作。",
        plan_context="预案2.2.2和2.2.5要求各小组分工明确、责任到人，接到预警后及时落实防控措施。",
    ),
    "总体应急目标 → 事故报告": TaskEnrichment(
        name="事故信息上报启动流程",
        description="信息联络组依照专项预案接收现场险情并向项目部、公司和政府主管部门逐级报告，为启动响应做准备。",
        plan_context="预案2.2.4规定现场处置小组先期处置同时上报项目部，应急领导小组据此评估险情并决定响应等级。",
    ),
    "总体应急目标 → 事故报告 → 事故发现": TaskEnrichment(
        name="现场人员立即报告险情",
        description="现场处置小组成员发现事故后第一时间通过通讯设备向项目部报告，说明地点、时间、伤亡及设备受损情况。",
        plan_context="预案2.2.4(1)明确要求发现人员立即报告，阐明事故地点、时间、设备受损程度和受伤人数。",
    ),
    "总体应急目标 → 事故报告 → 事故发现 → 报告内容确认": TaskEnrichment(
        name="明确位置时间损失情况",
        description="在汇报中逐项核实事故发生位置、具体时间、设备损坏及伤亡情况，确保上报信息完整准确。",
        plan_context="预案2.2.4(1)要求报告时准确说明事故地点、时间、设备受损程度和人员受伤情况。",
    ),
    "总体应急目标 → 事故报告 → 信息汇总": TaskEnrichment(
        name="项目部汇总并评估险情",
        description="信息联络组汇集现场反馈与监测数据，研判事故发展态势，为应急领导小组决策提供依据。",
        plan_context="预案2.2.4指出项目部应急救援领导小组接收险情报告后要立即评估并确定响应等级。",
    ),
    "总体应急目标 → 事故报告 → 信息汇总 → 核实信息": TaskEnrichment(
        name="与相关单位核实情况",
        description="与监理、建设单位及现场指挥保持通信，核对事故事实和资源需求，确保对外报告准确一致。",
        plan_context="预案2.2.4(2)要求应急组织人员赶赴现场并分工协作，保持通信联络掌握真实情况。",
    ),
    "总体应急目标 → 事故报告 → 上报政府": TaskEnrichment(
        name="向主管部门同步报告",
        description="信息联络组在项目部汇总后，按规定向交通、应急、海事等主管部门通报事故并请求指导支援。",
        plan_context="预案2.2.5.1(2)强调接报后立即向项目部、公司调度室以及就近海事局或海上搜救中心报告。",
    ),
    "总体应急目标 → 事故报告 → 上报政府 → 逐级汇报": TaskEnrichment(
        name="按要求逐级通报",
        description="依照政府应急体系逐级汇报事故性质、发展态势与所需支援，保持与外部主管部门的同步沟通。",
        plan_context="预案2.2.5.1(2)提出需向项目部、公司及海事部门报告，必要时联系海上搜救中心协助处置。",
    ),
    "总体应急目标 → 应急响应": TaskEnrichment(
        name="启动项目应急响应程序",
        description="项目部应急领导小组根据险情研判启动应急预案，组织各小组到位执行处置任务。",
        plan_context="预案2.2.4说明项目部应急救援领导小组接到报告后立即评估险情并决定响应等级、启动预案。",
    ),
    "总体应急目标 → 应急响应 → 分级响应": TaskEnrichment(
        name="研判并确定响应等级",
        description="综合事故规模与影响范围，判定启动项目内部Ⅱ级响应或升级为需要社会力量参与的Ⅰ级响应。",
        plan_context="预案2.2.4指出领导小组需根据评估结果确定响应等级并组织执行。",
    ),
    "总体应急目标 → 应急响应 → 分级响应 → 启动Ⅱ级响应": TaskEnrichment(
        name="组织项目内部自救处置",
        description="当事故可在项目范围内控制时，调集项目资源开展自救抢险、医疗救护与现场警戒。",
        plan_context="预案2.2.4提到现场处置小组先期处置并向项目部汇报，项目部可组织内部力量控制险情。",
    ),
    "总体应急目标 → 应急响应 → 分级响应 → 启动Ⅰ级响应": TaskEnrichment(
        name="协调外部救援力量介入",
        description="当险情超出项目能力时，请求政府、海事或搜救中心等外部专业力量增援，确保人命与船舶安全。",
        plan_context="预案2.2.5.1(2)、(12)强调必要时向海事局、搜救中心报告并请求第三方援助。",
    ),
    "总体应急目标 → 应急响应 → 任务分工": TaskEnrichment(
        name="明确各应急小组任务",
        description="根据预案将医疗救护、工程抢险、综合协调、信息联络等小组的职责迅速细化到岗位，形成作战部署。",
        plan_context="预案2.2.4(2)要求应急组织人员赶赴现场并同时安排救治、报警、封闭现场等多项任务。",
    ),
    "总体应急目标 → 应急响应 → 任务分工 → 下达指令": TaskEnrichment(
        name="向各小组下达现场处置指令",
        description="项目部领导根据现场研判向各小组发布抢险、救护、封控等指令，确保行动同步推进。",
        plan_context="预案2.2.4(2)描述接报后需要同时安排救治、报警、封闭现场等任务，体现指挥统一调度。",
    ),
    "总体应急目标 → 现场处置": TaskEnrichment(
        name="组织现场抢险救援行动",
        description="按照专项预案开展医疗救护、封闭事故区、调动设备等行动，控制险情发展。",
        plan_context="预案2.2.4(2)-(3)明确现场需同步组织救治、求援、封闭现场和调动设备抢救，尽量降低损失。",
    ),
    "总体应急目标 → 现场处置 → 人员救护": TaskEnrichment(
        name="安排受伤人员救治流程",
        description="根据伤情制定紧急救治方案，联系120并准备接应医护人员，保障伤员及时救治。",
        plan_context="预案2.2.4(2)①-②要求根据受伤程度采取紧急救治、向120求救并派人引导救护车到场。",
    ),
    "总体应急目标 → 现场处置 → 人员救护 → 现场急救": TaskEnrichment(
        name="实施现场紧急救护",
        description="医护与现场救援人员在专业救援抵达前持续开展包扎、止血、心肺复苏等急救，保障伤员生命体征稳定。",
        plan_context="预案2.2.4(2)②指出医务人员未接替救治前现场人员应及时组织抢救。",
    ),
    "总体应急目标 → 现场处置 → 人员救护 → 医院转运": TaskEnrichment(
        name="联系120并护送转运",
        description="拨打120抢救中心并派员在交通节点引导救护车，协助医务人员将伤员安全转运医院。",
        plan_context="预案2.2.4(2)②要求向120求救并派人在交叉路口指引救护车尽快到达。",
    ),
    "总体应急目标 → 现场处置 → 工程抢险": TaskEnrichment(
        name="调配装备进行工程抢险",
        description="组织抢险力量抢救事故设备和受困人员，采取排水、堵漏、加固等措施遏制险情扩散。",
        plan_context="预案2.2.4(3)强调组织人员调动设备抢救事故设备和人员；预案2.2.5.1(6)-(7)要求采取排水、堵漏等应对措施。",
    ),
    "总体应急目标 → 现场处置 → 工程抢险 → 封锁险区": TaskEnrichment(
        name="封锁现场设置警戒",
        description="在事故周边设置警戒带和警示标志，阻止无关人员进入，维护救援秩序。",
        plan_context="预案2.2.4(2)③要求封闭事故现场并设置警示标志，禁止无关人员进入。",
    ),
    "总体应急目标 → 现场处置 → 工程抢险 → 险情控制": TaskEnrichment(
        name="排水堵漏稳定结构",
        description="根据破损情况实施排水、堵漏、加固及必要时抢滩坐浅等措施，稳定船体和施工设备。",
        plan_context="预案2.2.5.1(6)-(7)和2.2.5.3(5)-(11)提出通过排水、堵漏、加固及抢滩等手段控制险情发展。",
    ),
    "总体应急目标 → 现场处置 → 综合协调": TaskEnrichment(
        name="协调交通疏导与现场秩序",
        description="综合协调组配合公安、交通等力量实施交通管制、人员疏散和现场保护，防止次生事故。",
        plan_context="预案2.2.4(2)③提示要封闭现场、设置警示并禁止无关人员进入，确保现场秩序。",
    ),
    "总体应急目标 → 现场处置 → 综合协调 → 疏散人员": TaskEnrichment(
        name="组织撤离非作业人员",
        description="指挥无关人员撤离事故区，安排安全集合点，保障救援通道畅通。",
        plan_context="预案2.2.4(2)③提出封闭现场并禁止无关人员进入，需要组织人员疏散。",
    ),
    "总体应急目标 → 现场处置 → 信息联络": TaskEnrichment(
        name="保持内外通信与信息传递",
        description="信息联络组同步向现场指挥、项目部和外部单位传递险情变化，确保指挥协调顺畅。",
        plan_context="预案2.2.4(2)②和2.2.5.2(13)要求保持与内外部的通信联络，及时通报情况。",
    ),
    "总体应急目标 → 现场处置 → 信息联络 → 外部联络": TaskEnrichment(
        name="对接医院海事等外部单位",
        description="持续联系120救援中心、海事局、搜救中心及相关政府部门，通报救援需求并协调支援力量。",
        plan_context="预案2.2.4(2)②、2.2.5.1(2)和2.2.5.2(4)要求向120、海事局等外部机构报告并保持联络。",
    ),
    "总体应急目标 → 后期恢复": TaskEnrichment(
        name="进入善后与恢复阶段",
        description="险情受控后转入善后、调查和总结工作，确保事故响应形成闭环管理。",
        plan_context="预案2.2.4(4)指出在伤员救护可靠、事故得到控制且资源到位后，应急救援行动结束并进入后续处置。",
    ),
    "总体应急目标 → 后期恢复 → 善后工作": TaskEnrichment(
        name="落实安置补偿和后勤保障",
        description="组织受影响人员的安置、补偿及心理支持，恢复生产生活秩序并调配保障资源。",
        plan_context="预案2.2.4(4)提示在事故控制并资源落实后转入善后处置，确保人员得到妥善安置。",
    ),
    "总体应急目标 → 后期恢复 → 善后工作 → 心理安抚": TaskEnrichment(
        name="提供心理慰藉与家属沟通",
        description="善后处置组安排心理疏导和家属沟通，帮助伤亡人员及家属稳定情绪。",
        plan_context="预案2.2.4(4)强调救援结束后进入善后阶段，需要保障人员获得必要支持。",
    ),
    "总体应急目标 → 后期恢复 → 事故调查": TaskEnrichment(
        name="组织事故调查与责任认定",
        description="联合技术支持组和相关机构查明事故原因、责任主体及改进措施，形成调查结论。",
        plan_context="预案2.2.5.1(3)-(4)、(9)-(11)要求查明破损情况、保存记录并向主管机关报告，为事故调查提供依据。",
    ),
    "总体应急目标 → 后期恢复 → 事故调查 → 资料收集": TaskEnrichment(
        name="收集日志影像等调查资料",
        description="系统整理航海日志、轮机日志、照片和监测记录，为事故调查和责任认定提供证据。",
        plan_context="预案2.2.5.1(9)-(10)强调保存被碰撞通知书、海图、损坏草图、照片及各类日志记录。",
    ),
    "总体应急目标 → 后期恢复 → 总结评估": TaskEnrichment(
        name="评估响应成效提出改进",
        description="对应急行动的组织、协同与资源保障进行复盘，总结经验教训并提出改进措施。",
        plan_context="预案2.2.4(4)要求在行动结束后落实人员物资并开展后续处置，为总结评估提供条件。",
    ),
    "总体应急目标 → 后期恢复 → 总结评估 → 编写报告": TaskEnrichment(
        name="形成总结评估书面报告",
        description="项目部应急领导小组编写事故应急处置报告，记录过程、成效与改进建议，归档入库。",
        plan_context="预案2.2.5.1(10)-(11)提出要做好详细记录并向主管机关报告，为形成总结报告提供依据。",
    ),
}

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


def apply_task_enrichments(
    root: TaskNode, enrichments: Mapping[str, TaskEnrichment]
) -> None:
    nodes = list(iterate_nodes(root))
    path_map = {" → ".join(node.path()): node for node in nodes}
    for path, enrichment in enrichments.items():
        node = path_map.get(path)
        if node is None:
            continue
        if enrichment.name:
            node.name = enrichment.name
        if enrichment.description:
            node.description = enrichment.description
        if enrichment.plan_context:
            node.plan_context = enrichment.plan_context


def build_lane_map(
    nodes: Sequence[TaskNode],
) -> Tuple[List[Dict[str, object]], Dict[str, str]]:
    lane_map: "OrderedDict[str, Dict[str, object]]" = OrderedDict()
    node_to_lane: Dict[str, str] = {}
    for node in nodes:
        responsibles = node.responsibles or ["未指定责任主体"]
        primary = responsibles[0]
        lane = lane_map.get(primary)
        if lane is None:
            lane = {
                "id": f"Lane_{len(lane_map) + 1}",
                "name": primary,
                "flow_nodes": [],
            }
            lane_map[primary] = lane
        lane["flow_nodes"].append(node.bpmn_id)
        node_to_lane[node.bpmn_id] = lane["id"]
        for additional in responsibles[1:]:
            if additional not in lane_map:
                lane_map[additional] = {
                    "id": f"Lane_{len(lane_map) + 1}",
                    "name": additional,
                    "flow_nodes": [],
                }
    return list(lane_map.values()), node_to_lane


def layout_positions(
    ordered_nodes: Sequence[TaskNode],
    lanes: Sequence[Dict[str, object]],
    node_to_lane: Dict[str, str],
    start_id: str,
    end_id: str,
) -> Tuple[
    Dict[str, Tuple[float, float, float, float]],
    Dict[str, Tuple[float, float, float, float]],
]:
    lane_x = 60.0
    lane_label_width = 140.0
    lane_gap = 20.0
    lane_height = 160.0
    task_width = 180.0
    task_height = 80.0
    task_gap_x = 220.0
    event_size = 36.0

    content_start_x = lane_x + lane_label_width + 40.0
    base_y = 60.0

    lane_positions: Dict[str, Tuple[float, float, float, float]] = {}
    lane_index: Dict[str, int] = {}
    for index, lane in enumerate(lanes):
        lane_id = lane["id"]
        lane_index[lane_id] = index
    if ordered_nodes:
        max_task_right = content_start_x + (len(ordered_nodes) - 1) * task_gap_x + task_width
    else:
        max_task_right = content_start_x + task_width
    lane_width = max_task_right + 80.0 - lane_x

    for lane_id, index in lane_index.items():
        lane_y = base_y + index * (lane_height + lane_gap)
        lane_positions[lane_id] = (lane_x, lane_y, lane_width, lane_height)

    positions: Dict[str, Tuple[float, float, float, float]] = {}

    for node_index, node in enumerate(ordered_nodes):
        lane_id = node_to_lane[node.bpmn_id]
        lane_y = lane_positions[lane_id][1]
        y_position = lane_y + (lane_height - task_height) / 2.0
        x_position = content_start_x + node_index * task_gap_x
        positions[node.bpmn_id] = (x_position, y_position, task_width, task_height)

    if ordered_nodes:
        first_lane_id = node_to_lane[ordered_nodes[0].bpmn_id]
        last_lane_id = node_to_lane[ordered_nodes[-1].bpmn_id]
        first_lane_y = lane_positions[first_lane_id][1]
        last_lane_y = lane_positions[last_lane_id][1]
    else:
        first_lane_y = base_y
        last_lane_y = base_y

    start_x = content_start_x - event_size - 40.0
    start_y = first_lane_y + (lane_height - event_size) / 2.0
    end_x = max_task_right + 40.0
    end_y = last_lane_y + (lane_height - event_size) / 2.0

    positions[start_id] = (start_x, start_y, event_size, event_size)
    positions[end_id] = (end_x, end_y, event_size, event_size)

    return positions, lane_positions


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

    lanes, node_to_lane = build_lane_map(ordered_nodes)

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
    for lane in lanes:
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
    positions, lane_positions = layout_positions(
        ordered_nodes, lanes, node_to_lane, start_id, end_id
    )
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
    for lane in lanes:
        lane_bounds = lane_positions[lane["id"]]
        lane_shape = ET.SubElement(
            plane,
            "bpmndi:BPMNShape",
            attrib={"id": f"{lane['id']}_di", "bpmnElement": lane["id"]},
        )
        ET.SubElement(
            lane_shape,
            "dc:Bounds",
            attrib={
                "x": f"{lane_bounds[0]:.2f}",
                "y": f"{lane_bounds[1]:.2f}",
                "width": f"{lane_bounds[2]:.2f}",
                "height": f"{lane_bounds[3]:.2f}",
            },
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
    def element_right(bounds: Tuple[float, float, float, float]) -> Tuple[float, float]:
        x, y, width, height = bounds
        return (x + width, y + height / 2.0)

    def element_left(bounds: Tuple[float, float, float, float]) -> Tuple[float, float]:
        x, y, _, height = bounds
        return (x, y + height / 2.0)
    for flow_id, source_id, target_id in sequence_flows:
        edge = ET.SubElement(
            plane,
            "bpmndi:BPMNEdge",
            attrib={"id": f"{flow_id}_di", "bpmnElement": flow_id},
        )
        source_bounds = positions[source_id]
        target_bounds = positions[target_id]
        if source_id == start_id:
            start_point = element_right(source_bounds)
        else:
            start_point = element_right(source_bounds)
        if target_id == end_id:
            end_point = element_left(target_bounds)
        else:
            end_point = element_left(target_bounds)
        for point in (start_point, end_point):
            ET.SubElement(
                edge,
                "di:waypoint",
                attrib={"x": f"{point[0]:.2f}", "y": f"{point[1]:.2f}"},
            )
    tree = ET.ElementTree(definitions)
    ET.indent(tree, space="  ", level=0)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write("\n")


def build_and_export(
    hta_path: Path,
    plan_path: Path,
    output_path: Path,
) -> None:
    root = parse_hta_csv(hta_path)
    paragraphs = read_plan_paragraphs(plan_path)
    assign_plan_context(root, paragraphs)
    apply_task_enrichments(root, TASK_ENRICHMENTS)
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
