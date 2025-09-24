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


@dataclass
class FlowNodeSpec:
    """Description of a BPMN flow node in the project process."""

    element_id: str
    element_type: str
    name: str
    lane: Optional[str]
    x: float
    task: Optional[TaskNode] = None
    y_offset: float = 0.0
    documentation: Optional[str] = None


@dataclass
class LaneSpec:
    """Metadata for a BPMN lane."""

    lane_id: str
    name: str
    index: int
    flow_nodes: List[str] = field(default_factory=list)


@dataclass
class DataObjectSpec:
    """Description of a data object reference in the BPMN model."""

    data_id: str
    ref_id: str
    name: str
    lane: str
    x: float
    y_offset: float = 0.0


TASK_ENRICHMENTS: Dict[str, TaskEnrichment] = {
    "总体应急目标 → 监测施工水域风险并预警": TaskEnrichment(
        name="监测施工水域风险并预警",
        description="综合协调组联合技术支持组围绕船员、船舶与环境因素评估碰撞、火灾、风浪、自沉等风险，提前调整施工计划并准备应对措施。",
        plan_context="预案2.2.2指出水上作业可能因船员因素、船舶因素和环境因素引发碰撞、火灾、风灾、自沉、搁浅等事故，需要提前识别风险并采取预警措施。",
    ),
    "总体应急目标 → 监测施工水域风险并预警 → 巡查施工与航道风险点": TaskEnrichment(
        name="巡查施工与航道风险点",
        description="技术支持组对施工海域的船舶运行、作业干扰和周边航行环境实施巡查，跟踪可能诱发水上交通事故的风险点。",
        plan_context="预案2.2.2(1)-(7)列举了船舶碰撞、火灾爆炸、风浪损伤、自沉、搁浅及人员落水等风险情形，要求持续监测施工与航道状况。",
    ),
    "总体应急目标 → 监测施工水域风险并预警 → 巡查施工与航道风险点 → 检查船舶与设备状态": TaskEnrichment(
        name="检查船舶与设备状态",
        description="落实船舶机舱、泵舱、消防、通信等设备的例行检查，发现设备异常及时调整，避免因设备缺陷诱发事故。",
        plan_context="预案2.2.2强调船舶因素可能引发火灾、爆炸、自沉等险情，需要通过日常巡查掌握设备运行状态。",
    ),
    "总体应急目标 → 监测施工水域风险并预警 → 巡查施工与航道风险点 → 掌握气象与水文条件": TaskEnrichment(
        name="掌握气象与水文条件",
        description="跟踪风力、突风、潮差和能见度等环境条件，及时采取避险或调整航行安排，降低风灾和浪损风险。",
        plan_context="预案2.2.2(4)提示海上施工易受大风、突风影响发生风灾、浪损事故，需要加强环境监测。",
    ),
    "总体应急目标 → 监测施工水域风险并预警 → 发布预警等级与调整施工": TaskEnrichment(
        name="发布预警等级与调整施工",
        description="项目部应急办公室依据风险巡查结果划分蓝、黄、橙、红预警等级，通知相关单位调整作业强度并做好防范部署。",
        plan_context="预案要求根据监测到的碰撞、火灾、自沉等风险提前发布预警信息，引导施工单位采取防范措施。",
    ),
    "总体应急目标 → 监测施工水域风险并预警 → 发布预警等级与调整施工 → 通知各小组执行预警措施": TaskEnrichment(
        name="通知各小组执行预警措施",
        description="综合协调组向各应急小组通报预警等级，明确值守、避风、减载、调整航次等协同动作。",
        plan_context="预案2.2.2和2.2.5要求各小组分工明确、责任到人，接到预警后及时落实防控措施。",
    ),
    "总体应急目标 → 事故信息上报启动流程": TaskEnrichment(
        name="事故信息上报启动流程",
        description="信息联络组依照专项预案接收现场险情并向项目部、公司和政府主管部门逐级报告，为启动响应做准备。",
        plan_context="预案2.2.4规定现场处置小组先期处置同时上报项目部，应急领导小组据此评估险情并决定响应等级。",
    ),
    "总体应急目标 → 事故信息上报启动流程 → 现场人员立即报告险情": TaskEnrichment(
        name="现场人员立即报告险情",
        description="现场处置小组成员发现事故后第一时间通过通讯设备向项目部报告，说明地点、时间、伤亡及设备受损情况。",
        plan_context="预案2.2.4(1)明确要求发现人员立即报告，阐明事故地点、时间、设备受损程度和受伤人数。",
    ),
    "总体应急目标 → 事故信息上报启动流程 → 现场人员立即报告险情 → 明确位置时间损失情况": TaskEnrichment(
        name="明确位置时间损失情况",
        description="在汇报中逐项核实事故发生位置、具体时间、设备损坏及伤亡情况，确保上报信息完整准确。",
        plan_context="预案2.2.4(1)要求报告时准确说明事故地点、时间、设备受损程度和人员受伤情况。",
    ),
    "总体应急目标 → 事故信息上报启动流程 → 项目部汇总并评估险情": TaskEnrichment(
        name="项目部汇总并评估险情",
        description="信息联络组汇集现场反馈与监测数据，研判事故发展态势，为应急领导小组决策提供依据。",
        plan_context="预案2.2.4指出项目部应急救援领导小组接收险情报告后要立即评估并确定响应等级。",
    ),
    "总体应急目标 → 事故信息上报启动流程 → 项目部汇总并评估险情 → 与相关单位核实情况": TaskEnrichment(
        name="与相关单位核实情况",
        description="与监理、建设单位及现场指挥保持通信，核对事故事实和资源需求，确保对外报告准确一致。",
        plan_context="预案2.2.4(2)要求应急组织人员赶赴现场并分工协作，保持通信联络掌握真实情况。",
    ),
    "总体应急目标 → 事故信息上报启动流程 → 向主管部门同步报告": TaskEnrichment(
        name="向主管部门同步报告",
        description="信息联络组在项目部汇总后，按规定向交通、应急、海事等主管部门通报事故并请求指导支援。",
        plan_context="预案2.2.5.1(2)强调接报后立即向项目部、公司调度室以及就近海事局或海上搜救中心报告。",
    ),
    "总体应急目标 → 事故信息上报启动流程 → 向主管部门同步报告 → 按要求逐级通报": TaskEnrichment(
        name="按要求逐级通报",
        description="依照政府应急体系逐级汇报事故性质、发展态势与所需支援，保持与外部主管部门的同步沟通。",
        plan_context="预案2.2.5.1(2)提出需向项目部、公司及海事部门报告，必要时联系海上搜救中心协助处置。",
    ),
    "总体应急目标 → 启动项目应急响应程序": TaskEnrichment(
        name="启动项目应急响应程序",
        description="项目部应急领导小组根据险情研判启动应急预案，组织各小组到位执行处置任务。",
        plan_context="预案2.2.4说明项目部应急救援领导小组接到报告后立即评估险情并决定响应等级、启动预案。",
    ),
    "总体应急目标 → 启动项目应急响应程序 → 研判并确定响应等级": TaskEnrichment(
        name="研判并确定响应等级",
        description="综合事故规模与影响范围，判定启动项目内部Ⅱ级响应或升级为需要社会力量参与的Ⅰ级响应。",
        plan_context="预案2.2.4指出领导小组需根据评估结果确定响应等级并组织执行。",
    ),
    "总体应急目标 → 启动项目应急响应程序 → 研判并确定响应等级 → 组织项目内部自救处置": TaskEnrichment(
        name="组织项目内部自救处置",
        description="当事故可在项目范围内控制时，调集项目资源开展自救抢险、医疗救护与现场警戒。",
        plan_context="预案2.2.4提到现场处置小组先期处置并向项目部汇报，项目部可组织内部力量控制险情。",
    ),
    "总体应急目标 → 启动项目应急响应程序 → 研判并确定响应等级 → 协调外部救援力量介入": TaskEnrichment(
        name="协调外部救援力量介入",
        description="当险情超出项目能力时，请求政府、海事或搜救中心等外部专业力量增援，确保人命与船舶安全。",
        plan_context="预案2.2.5.1(2)、(12)强调必要时向海事局、搜救中心报告并请求第三方援助。",
    ),
    "总体应急目标 → 启动项目应急响应程序 → 明确各应急小组任务": TaskEnrichment(
        name="明确各应急小组任务",
        description="根据预案将医疗救护、工程抢险、综合协调、信息联络等小组的职责迅速细化到岗位，形成作战部署。",
        plan_context="预案2.2.4(2)要求应急组织人员赶赴现场并同时安排救治、报警、封闭现场等多项任务。",
    ),
    "总体应急目标 → 启动项目应急响应程序 → 明确各应急小组任务 → 向各小组下达现场处置指令": TaskEnrichment(
        name="向各小组下达现场处置指令",
        description="项目部领导根据现场研判向各小组发布抢险、救护、封控等指令，确保行动同步推进。",
        plan_context="预案2.2.4(2)描述接报后需要同时安排救治、报警、封闭现场等任务，体现指挥统一调度。",
    ),
    "总体应急目标 → 组织现场抢险救援行动": TaskEnrichment(
        name="组织现场抢险救援行动",
        description="按照专项预案开展医疗救护、封闭事故区、调动设备等行动，控制险情发展。",
        plan_context="预案2.2.4(2)-(3)明确现场需同步组织救治、求援、封闭现场和调动设备抢救，尽量降低损失。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 安排受伤人员救治流程": TaskEnrichment(
        name="安排受伤人员救治流程",
        description="根据伤情制定紧急救治方案，联系120并准备接应医护人员，保障伤员及时救治。",
        plan_context="预案2.2.4(2)①-②要求根据受伤程度采取紧急救治、向120求救并派人引导救护车到场。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 安排受伤人员救治流程 → 实施现场紧急救护": TaskEnrichment(
        name="实施现场紧急救护",
        description="医护与现场救援人员在专业救援抵达前持续开展包扎、止血、心肺复苏等急救，保障伤员生命体征稳定。",
        plan_context="预案2.2.4(2)②指出医务人员未接替救治前现场人员应及时组织抢救。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 安排受伤人员救治流程 → 联系120并护送转运": TaskEnrichment(
        name="联系120并护送转运",
        description="拨打120抢救中心并派员在交通节点引导救护车，协助医务人员将伤员安全转运医院。",
        plan_context="预案2.2.4(2)②要求向120求救并派人在交叉路口指引救护车尽快到达。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 调配装备进行工程抢险": TaskEnrichment(
        name="调配装备进行工程抢险",
        description="组织抢险力量抢救事故设备和受困人员，采取排水、堵漏、加固等措施遏制险情扩散。",
        plan_context="预案2.2.4(3)强调组织人员调动设备抢救事故设备和人员；预案2.2.5.1(6)-(7)要求采取排水、堵漏等应对措施。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 调配装备进行工程抢险 → 封锁现场设置警戒": TaskEnrichment(
        name="封锁现场设置警戒",
        description="在事故周边设置警戒带和警示标志，阻止无关人员进入，维护救援秩序。",
        plan_context="预案2.2.4(2)③要求封闭事故现场并设置警示标志，禁止无关人员进入。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 调配装备进行工程抢险 → 排水堵漏稳定结构": TaskEnrichment(
        name="排水堵漏稳定结构",
        description="根据破损情况实施排水、堵漏、加固及必要时抢滩坐浅等措施，稳定船体和施工设备。",
        plan_context="预案2.2.5.1(6)-(7)和2.2.5.3(5)-(11)提出通过排水、堵漏、加固及抢滩等手段控制险情发展。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 协调交通疏导与现场秩序": TaskEnrichment(
        name="协调交通疏导与现场秩序",
        description="综合协调组配合公安、交通等力量实施交通管制、人员疏散和现场保护，防止次生事故。",
        plan_context="预案2.2.4(2)③提示要封闭现场、设置警示并禁止无关人员进入，确保现场秩序。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 协调交通疏导与现场秩序 → 组织撤离非作业人员": TaskEnrichment(
        name="组织撤离非作业人员",
        description="指挥无关人员撤离事故区，安排安全集合点，保障救援通道畅通。",
        plan_context="预案2.2.4(2)③提出封闭现场并禁止无关人员进入，需要组织人员疏散。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 保持内外通信与信息传递": TaskEnrichment(
        name="保持内外通信与信息传递",
        description="信息联络组同步向现场指挥、项目部和外部单位传递险情变化，确保指挥协调顺畅。",
        plan_context="预案2.2.4(2)②和2.2.5.2(13)要求保持与内外部的通信联络，及时通报情况。",
    ),
    "总体应急目标 → 组织现场抢险救援行动 → 保持内外通信与信息传递 → 对接医院海事等外部单位": TaskEnrichment(
        name="对接医院海事等外部单位",
        description="持续联系120救援中心、海事局、搜救中心及相关政府部门，通报救援需求并协调支援力量。",
        plan_context="预案2.2.4(2)②、2.2.5.1(2)和2.2.5.2(4)要求向120、海事局等外部机构报告并保持联络。",
    ),
    "总体应急目标 → 进入善后与恢复阶段": TaskEnrichment(
        name="进入善后与恢复阶段",
        description="险情受控后转入善后、调查和总结工作，确保事故响应形成闭环管理。",
        plan_context="预案2.2.4(4)指出在伤员救护可靠、事故得到控制且资源到位后，应急救援行动结束并进入后续处置。",
    ),
    "总体应急目标 → 进入善后与恢复阶段 → 落实安置补偿和后勤保障": TaskEnrichment(
        name="落实安置补偿和后勤保障",
        description="组织受影响人员的安置、补偿及心理支持，恢复生产生活秩序并调配保障资源。",
        plan_context="预案2.2.4(4)提示在事故控制并资源落实后转入善后处置，确保人员得到妥善安置。",
    ),
    "总体应急目标 → 进入善后与恢复阶段 → 落实安置补偿和后勤保障 → 提供心理慰藉与家属沟通": TaskEnrichment(
        name="提供心理慰藉与家属沟通",
        description="善后处置组安排心理疏导和家属沟通，帮助伤亡人员及家属稳定情绪。",
        plan_context="预案2.2.4(4)强调救援结束后进入善后阶段，需要保障人员获得必要支持。",
    ),
    "总体应急目标 → 进入善后与恢复阶段 → 组织事故调查与责任认定": TaskEnrichment(
        name="组织事故调查与责任认定",
        description="联合技术支持组和相关机构查明事故原因、责任主体及改进措施，形成调查结论。",
        plan_context="预案2.2.5.1(3)-(4)、(9)-(11)要求查明破损情况、保存记录并向主管机关报告，为事故调查提供依据。",
    ),
    "总体应急目标 → 进入善后与恢复阶段 → 组织事故调查与责任认定 → 收集日志影像等调查资料": TaskEnrichment(
        name="收集日志影像等调查资料",
        description="系统整理航海日志、轮机日志、照片和监测记录，为事故调查和责任认定提供证据。",
        plan_context="预案2.2.5.1(9)-(10)强调保存被碰撞通知书、海图、损坏草图、照片及各类日志记录。",
    ),
    "总体应急目标 → 进入善后与恢复阶段 → 评估响应成效提出改进": TaskEnrichment(
        name="评估响应成效提出改进",
        description="对应急行动的组织、协同与资源保障进行复盘，总结经验教训并提出改进措施。",
        plan_context="预案2.2.4(4)要求在行动结束后落实人员物资并开展后续处置，为总结评估提供条件。",
    ),
    "总体应急目标 → 进入善后与恢复阶段 → 评估响应成效提出改进 → 形成总结评估书面报告": TaskEnrichment(
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




def build_path_map(root: TaskNode) -> Dict[str, TaskNode]:
    """Return a mapping from HTA path strings to task nodes."""

    path_map: Dict[str, TaskNode] = {}

    def visit(node: TaskNode, prefix: List[str]) -> None:
        for child in node.children:
            new_prefix = prefix + [child.name]
            path_map[" → ".join(new_prefix)] = child
            visit(child, new_prefix)

    visit(root, [])
    return path_map


def connection_points(
    source_bounds: Tuple[float, float, float, float],
    target_bounds: Tuple[float, float, float, float],
) -> List[Tuple[float, float]]:
    """Return two points describing a straight connector between shapes."""

    sx, sy, sw, sh = source_bounds
    tx, ty, tw, th = target_bounds
    if sx + sw <= tx:
        return [(sx + sw, sy + sh / 2.0), (tx, ty + th / 2.0)]
    if tx + tw <= sx:
        return [(sx, sy + sh / 2.0), (tx + tw, ty + th / 2.0)]
    if sy + sh <= ty:
        return [(sx + sw / 2.0, sy + sh), (tx + tw / 2.0, ty)]
    return [(sx + sw / 2.0, sy), (tx + tw / 2.0, ty + th)]


def build_bpmn(root: TaskNode, output_path: Path) -> None:
    path_map = build_path_map(root)

    def task_node(path: str) -> TaskNode:
        try:
            return path_map[path]
        except KeyError as exc:
            raise KeyError(f"HTA path '{path}' was not found when building the BPMN model") from exc

    process_id = "Process_WaterTrafficEmergency"
    external_process_id = "Process_ExternalSupport"
    collaboration_id = "Collaboration_WaterTrafficEmergency"
    start_id = "StartEvent_RoutineMonitoring"
    end_id = "EndEvent_ResponseComplete"

    lane_order = [
        "技术支持组",
        "信息联络组",
        "综合协调组",
        "现场处置小组",
        "项目部应急领导小组",
        "项目部领导",
        "工程抢险组",
        "医疗救护组",
        "善后处置组",
    ]

    lanes: List[LaneSpec] = [
        LaneSpec(lane_id=f"Lane_{index + 1}", name=name, index=index)
        for index, name in enumerate(lane_order)
    ]
    lane_by_name = {lane.name: lane for lane in lanes}

    project_nodes: List[FlowNodeSpec] = [
        FlowNodeSpec(
            element_id=start_id,
            element_type="bpmn:startEvent",
            name="常态监测",
            lane="技术支持组",
            x=160.0,
            documentation="技术支持组开展航道施工常态巡查与风险监测的起点。",
        ),
        FlowNodeSpec(
            element_id="Task_PreventiveWatch",
            element_type="bpmn:task",
            name=task_node("监测施工水域风险并预警").name,
            lane="技术支持组",
            x=280.0,
            task=task_node("监测施工水域风险并预警"),
        ),
        FlowNodeSpec(
            element_id="Task_RiskPatrol",
            element_type="bpmn:task",
            name=task_node("监测施工水域风险并预警 → 巡查施工与航道风险点").name,
            lane="技术支持组",
            x=420.0,
            task=task_node("监测施工水域风险并预警 → 巡查施工与航道风险点"),
        ),
        FlowNodeSpec(
            element_id="Gateway_PreventiveSplit",
            element_type="bpmn:parallelGateway",
            name="风险监测分工",
            lane="技术支持组",
            x=560.0,
        ),
        FlowNodeSpec(
            element_id="Task_InspectEquipment",
            element_type="bpmn:task",
            name=task_node("监测施工水域风险并预警 → 巡查施工与航道风险点 → 检查船舶与设备状态").name,
            lane="技术支持组",
            x=700.0,
            y_offset=-24.0,
            task=task_node("监测施工水域风险并预警 → 巡查施工与航道风险点 → 检查船舶与设备状态"),
        ),
        FlowNodeSpec(
            element_id="Task_MonitorEnvironment",
            element_type="bpmn:task",
            name=task_node("监测施工水域风险并预警 → 巡查施工与航道风险点 → 掌握气象与水文条件").name,
            lane="技术支持组",
            x=700.0,
            y_offset=24.0,
            task=task_node("监测施工水域风险并预警 → 巡查施工与航道风险点 → 掌握气象与水文条件"),
        ),
        FlowNodeSpec(
            element_id="Gateway_PreventiveJoin",
            element_type="bpmn:parallelGateway",
            name="监测信息汇总",
            lane="技术支持组",
            x=840.0,
        ),
        FlowNodeSpec(
            element_id="Task_PublishAlert",
            element_type="bpmn:task",
            name=task_node("监测施工水域风险并预警 → 发布预警等级与调整施工").name,
            lane="信息联络组",
            x=980.0,
            task=task_node("监测施工水域风险并预警 → 发布预警等级与调整施工"),
        ),
        FlowNodeSpec(
            element_id="Task_NotifyTeams",
            element_type="bpmn:task",
            name=task_node("监测施工水域风险并预警 → 发布预警等级与调整施工 → 通知各小组执行预警措施").name,
            lane="综合协调组",
            x=1120.0,
            task=task_node("监测施工水域风险并预警 → 发布预警等级与调整施工 → 通知各小组执行预警措施"),
        ),
        FlowNodeSpec(
            element_id="IntermediateEvent_Incident",
            element_type="bpmn:intermediateCatchEvent",
            name="事故发生",
            lane="现场处置小组",
            x=1260.0,
            documentation="施工现场突发水上交通事故，触发应急程序。",
        ),
        FlowNodeSpec(
            element_id="Task_ReportProcess",
            element_type="bpmn:task",
            name=task_node("事故信息上报启动流程").name,
            lane="现场处置小组",
            x=1380.0,
            task=task_node("事故信息上报启动流程"),
        ),
        FlowNodeSpec(
            element_id="Task_ReportIncident",
            element_type="bpmn:task",
            name=task_node("事故信息上报启动流程 → 现场人员立即报告险情").name,
            lane="现场处置小组",
            x=1520.0,
            task=task_node("事故信息上报启动流程 → 现场人员立即报告险情"),
        ),
        FlowNodeSpec(
            element_id="Task_ConfirmDetails",
            element_type="bpmn:task",
            name=task_node("事故信息上报启动流程 → 现场人员立即报告险情 → 明确位置时间损失情况").name,
            lane="现场处置小组",
            x=1660.0,
            task=task_node("事故信息上报启动流程 → 现场人员立即报告险情 → 明确位置时间损失情况"),
        ),
        FlowNodeSpec(
            element_id="Task_SummariseSituation",
            element_type="bpmn:task",
            name=task_node("事故信息上报启动流程 → 项目部汇总并评估险情").name,
            lane="信息联络组",
            x=1800.0,
            task=task_node("事故信息上报启动流程 → 项目部汇总并评估险情"),
        ),
        FlowNodeSpec(
            element_id="Task_VerifyWithUnits",
            element_type="bpmn:task",
            name=task_node("事故信息上报启动流程 → 项目部汇总并评估险情 → 与相关单位核实情况").name,
            lane="信息联络组",
            x=1940.0,
            task=task_node("事故信息上报启动流程 → 项目部汇总并评估险情 → 与相关单位核实情况"),
        ),
        FlowNodeSpec(
            element_id="Task_ReportAuthorities",
            element_type="bpmn:task",
            name=task_node("事故信息上报启动流程 → 向主管部门同步报告").name,
            lane="信息联络组",
            x=2080.0,
            task=task_node("事故信息上报启动流程 → 向主管部门同步报告"),
        ),
        FlowNodeSpec(
            element_id="Task_EscalateAuthorities",
            element_type="bpmn:task",
            name=task_node("事故信息上报启动流程 → 向主管部门同步报告 → 按要求逐级通报").name,
            lane="信息联络组",
            x=2220.0,
            task=task_node("事故信息上报启动流程 → 向主管部门同步报告 → 按要求逐级通报"),
        ),
        FlowNodeSpec(
            element_id="Task_InitiateResponse",
            element_type="bpmn:task",
            name=task_node("启动项目应急响应程序").name,
            lane="项目部应急领导小组",
            x=2360.0,
            task=task_node("启动项目应急响应程序"),
        ),
        FlowNodeSpec(
            element_id="Task_AssessResponseLevel",
            element_type="bpmn:task",
            name=task_node("启动项目应急响应程序 → 研判并确定响应等级").name,
            lane="项目部应急领导小组",
            x=2500.0,
            task=task_node("启动项目应急响应程序 → 研判并确定响应等级"),
        ),
        FlowNodeSpec(
            element_id="Gateway_ResponseLevel",
            element_type="bpmn:exclusiveGateway",
            name="响应等级判定",
            lane="项目部应急领导小组",
            x=2640.0,
            documentation="根据核实后的险情，判断是否需要升级为社会救援级别。",
        ),
        FlowNodeSpec(
            element_id="Task_InternalResponse",
            element_type="bpmn:task",
            name=task_node("启动项目应急响应程序 → 研判并确定响应等级 → 组织项目内部自救处置").name,
            lane="项目部应急领导小组",
            x=2780.0,
            y_offset=-28.0,
            task=task_node("启动项目应急响应程序 → 研判并确定响应等级 → 组织项目内部自救处置"),
        ),
        FlowNodeSpec(
            element_id="Task_RequestExternalSupport",
            element_type="bpmn:task",
            name=task_node("启动项目应急响应程序 → 研判并确定响应等级 → 协调外部救援力量介入").name,
            lane="项目部应急领导小组",
            x=2780.0,
            y_offset=28.0,
            task=task_node("启动项目应急响应程序 → 研判并确定响应等级 → 协调外部救援力量介入"),
        ),
        FlowNodeSpec(
            element_id="Gateway_ResponseMerge",
            element_type="bpmn:exclusiveGateway",
            name="响应路径汇合",
            lane="项目部应急领导小组",
            x=2920.0,
        ),
        FlowNodeSpec(
            element_id="Task_DefineTeamMissions",
            element_type="bpmn:task",
            name=task_node("启动项目应急响应程序 → 明确各应急小组任务").name,
            lane="项目部应急领导小组",
            x=3060.0,
            task=task_node("启动项目应急响应程序 → 明确各应急小组任务"),
        ),
        FlowNodeSpec(
            element_id="Task_IssueOrders",
            element_type="bpmn:task",
            name=task_node("启动项目应急响应程序 → 明确各应急小组任务 → 向各小组下达现场处置指令").name,
            lane="项目部领导",
            x=3200.0,
            task=task_node("启动项目应急响应程序 → 明确各应急小组任务 → 向各小组下达现场处置指令"),
        ),
        FlowNodeSpec(
            element_id="Task_OnsiteOperations",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动").name,
            lane="项目部领导",
            x=3340.0,
            task=task_node("组织现场抢险救援行动"),
        ),
        FlowNodeSpec(
            element_id="Gateway_ParallelSplit",
            element_type="bpmn:parallelGateway",
            name="指令同步各小组",
            lane="项目部领导",
            x=3480.0,
        ),
        FlowNodeSpec(
            element_id="Task_MedicalPlan",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 安排受伤人员救治流程").name,
            lane="医疗救护组",
            x=3620.0,
            task=task_node("组织现场抢险救援行动 → 安排受伤人员救治流程"),
        ),
        FlowNodeSpec(
            element_id="Task_MedicalAid",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 安排受伤人员救治流程 → 实施现场紧急救护").name,
            lane="医疗救护组",
            x=3760.0,
            task=task_node("组织现场抢险救援行动 → 安排受伤人员救治流程 → 实施现场紧急救护"),
        ),
        FlowNodeSpec(
            element_id="Task_MedicalTransfer",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 安排受伤人员救治流程 → 联系120并护送转运").name,
            lane="医疗救护组",
            x=3900.0,
            task=task_node("组织现场抢险救援行动 → 安排受伤人员救治流程 → 联系120并护送转运"),
        ),
        FlowNodeSpec(
            element_id="Task_EngineeringDeploy",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 调配装备进行工程抢险").name,
            lane="工程抢险组",
            x=3620.0,
            task=task_node("组织现场抢险救援行动 → 调配装备进行工程抢险"),
        ),
        FlowNodeSpec(
            element_id="Task_BlockArea",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 调配装备进行工程抢险 → 封锁现场设置警戒").name,
            lane="工程抢险组",
            x=3760.0,
            task=task_node("组织现场抢险救援行动 → 调配装备进行工程抢险 → 封锁现场设置警戒"),
        ),
        FlowNodeSpec(
            element_id="Task_EngineeringControl",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 调配装备进行工程抢险 → 排水堵漏稳定结构").name,
            lane="工程抢险组",
            x=3900.0,
            task=task_node("组织现场抢险救援行动 → 调配装备进行工程抢险 → 排水堵漏稳定结构"),
        ),
        FlowNodeSpec(
            element_id="Task_Coordination",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 协调交通疏导与现场秩序").name,
            lane="综合协调组",
            x=3620.0,
            task=task_node("组织现场抢险救援行动 → 协调交通疏导与现场秩序"),
        ),
        FlowNodeSpec(
            element_id="Task_Evacuation",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 协调交通疏导与现场秩序 → 组织撤离非作业人员").name,
            lane="综合协调组",
            x=3760.0,
            task=task_node("组织现场抢险救援行动 → 协调交通疏导与现场秩序 → 组织撤离非作业人员"),
        ),
        FlowNodeSpec(
            element_id="Task_Communications",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 保持内外通信与信息传递").name,
            lane="信息联络组",
            x=3620.0,
            task=task_node("组织现场抢险救援行动 → 保持内外通信与信息传递"),
        ),
        FlowNodeSpec(
            element_id="Task_ExternalLiaison",
            element_type="bpmn:task",
            name=task_node("组织现场抢险救援行动 → 保持内外通信与信息传递 → 对接医院海事等外部单位").name,
            lane="信息联络组",
            x=3900.0,
            task=task_node("组织现场抢险救援行动 → 保持内外通信与信息传递 → 对接医院海事等外部单位"),
        ),
        FlowNodeSpec(
            element_id="Gateway_ParallelJoin",
            element_type="bpmn:parallelGateway",
            name="现场行动反馈",
            lane="项目部领导",
            x=4040.0,
        ),
        FlowNodeSpec(
            element_id="Task_RecoveryTransition",
            element_type="bpmn:task",
            name=task_node("进入善后与恢复阶段").name,
            lane="项目部应急领导小组",
            x=4180.0,
            task=task_node("进入善后与恢复阶段"),
        ),
        FlowNodeSpec(
            element_id="Gateway_RecoverySplit",
            element_type="bpmn:parallelGateway",
            name="恢复任务分流",
            lane="项目部应急领导小组",
            x=4320.0,
        ),
        FlowNodeSpec(
            element_id="Task_Aftercare",
            element_type="bpmn:task",
            name=task_node("进入善后与恢复阶段 → 落实安置补偿和后勤保障").name,
            lane="善后处置组",
            x=4460.0,
            task=task_node("进入善后与恢复阶段 → 落实安置补偿和后勤保障"),
        ),
        FlowNodeSpec(
            element_id="Task_PsychologicalSupport",
            element_type="bpmn:task",
            name=task_node("进入善后与恢复阶段 → 落实安置补偿和后勤保障 → 提供心理慰藉与家属沟通").name,
            lane="善后处置组",
            x=4600.0,
            task=task_node("进入善后与恢复阶段 → 落实安置补偿和后勤保障 → 提供心理慰藉与家属沟通"),
        ),
        FlowNodeSpec(
            element_id="Task_Investigation",
            element_type="bpmn:task",
            name=task_node("进入善后与恢复阶段 → 组织事故调查与责任认定").name,
            lane="技术支持组",
            x=4460.0,
            task=task_node("进入善后与恢复阶段 → 组织事故调查与责任认定"),
        ),
        FlowNodeSpec(
            element_id="Task_CollectEvidence",
            element_type="bpmn:task",
            name=task_node("进入善后与恢复阶段 → 组织事故调查与责任认定 → 收集日志影像等调查资料").name,
            lane="技术支持组",
            x=4600.0,
            task=task_node("进入善后与恢复阶段 → 组织事故调查与责任认定 → 收集日志影像等调查资料"),
        ),
        FlowNodeSpec(
            element_id="Gateway_RecoveryMerge",
            element_type="bpmn:parallelGateway",
            name="恢复任务汇合",
            lane="项目部应急领导小组",
            x=4740.0,
        ),
        FlowNodeSpec(
            element_id="Task_EvaluateResponse",
            element_type="bpmn:task",
            name=task_node("进入善后与恢复阶段 → 评估响应成效提出改进").name,
            lane="项目部应急领导小组",
            x=4880.0,
            task=task_node("进入善后与恢复阶段 → 评估响应成效提出改进"),
        ),
        FlowNodeSpec(
            element_id="Task_FinalReport",
            element_type="bpmn:task",
            name=task_node("进入善后与恢复阶段 → 评估响应成效提出改进 → 形成总结评估书面报告").name,
            lane="项目部应急领导小组",
            x=5020.0,
            task=task_node("进入善后与恢复阶段 → 评估响应成效提出改进 → 形成总结评估书面报告"),
        ),
        FlowNodeSpec(
            element_id=end_id,
            element_type="bpmn:endEvent",
            name="响应结束",
            lane="项目部应急领导小组",
            x=5160.0,
        ),
    ]

    for spec in project_nodes:
        if spec.lane is not None:
            lane_by_name[spec.lane].flow_nodes.append(spec.element_id)

    data_objects: List[DataObjectSpec] = [
        DataObjectSpec(
            data_id="Data_IncidentReport",
            ref_id="DataRef_IncidentReport",
            name="险情快报",
            lane="信息联络组",
            x=1740.0,
            y_offset=-60.0,
        ),
        DataObjectSpec(
            data_id="Data_ResponseOrders",
            ref_id="DataRef_ResponseOrders",
            name="响应指令",
            lane="项目部领导",
            x=3260.0,
            y_offset=-50.0,
        ),
        DataObjectSpec(
            data_id="Data_MedicalRecord",
            ref_id="DataRef_MedicalRecord",
            name="医疗救护记录",
            lane="医疗救护组",
            x=3940.0,
            y_offset=-10.0,
        ),
        DataObjectSpec(
            data_id="Data_EngineeringLog",
            ref_id="DataRef_EngineeringLog",
            name="工程抢险记录",
            lane="工程抢险组",
            x=3940.0,
            y_offset=40.0,
        ),
        DataObjectSpec(
            data_id="Data_StatusUpdates",
            ref_id="DataRef_StatusUpdates",
            name="现场联络日志",
            lane="信息联络组",
            x=3980.0,
            y_offset=60.0,
        ),
        DataObjectSpec(
            data_id="Data_FinalReport",
            ref_id="DataRef_FinalReport",
            name="事故总结报告",
            lane="项目部应急领导小组",
            x=5080.0,
        ),
    ]

    sequence_flows = [
        {"id": "Flow_Start_Preventive", "source": start_id, "target": "Task_PreventiveWatch"},
        {"id": "Flow_Preventive_RiskPatrol", "source": "Task_PreventiveWatch", "target": "Task_RiskPatrol"},
        {"id": "Flow_RiskPatrol_Split", "source": "Task_RiskPatrol", "target": "Gateway_PreventiveSplit"},
        {"id": "Flow_Split_Inspect", "source": "Gateway_PreventiveSplit", "target": "Task_InspectEquipment"},
        {"id": "Flow_Split_Environment", "source": "Gateway_PreventiveSplit", "target": "Task_MonitorEnvironment"},
        {"id": "Flow_Inspect_Join", "source": "Task_InspectEquipment", "target": "Gateway_PreventiveJoin"},
        {"id": "Flow_Environment_Join", "source": "Task_MonitorEnvironment", "target": "Gateway_PreventiveJoin"},
        {"id": "Flow_PreventiveJoin_Publish", "source": "Gateway_PreventiveJoin", "target": "Task_PublishAlert"},
        {"id": "Flow_Publish_Notify", "source": "Task_PublishAlert", "target": "Task_NotifyTeams"},
        {"id": "Flow_Notify_Incident", "source": "Task_NotifyTeams", "target": "IntermediateEvent_Incident"},
        {"id": "Flow_Incident_ReportProcess", "source": "IntermediateEvent_Incident", "target": "Task_ReportProcess"},
        {"id": "Flow_ReportProcess_ReportIncident", "source": "Task_ReportProcess", "target": "Task_ReportIncident"},
        {"id": "Flow_Report_Confirm", "source": "Task_ReportIncident", "target": "Task_ConfirmDetails"},
        {"id": "Flow_Confirm_Summarise", "source": "Task_ConfirmDetails", "target": "Task_SummariseSituation"},
        {"id": "Flow_Summarise_Verify", "source": "Task_SummariseSituation", "target": "Task_VerifyWithUnits"},
        {"id": "Flow_Verify_Report", "source": "Task_VerifyWithUnits", "target": "Task_ReportAuthorities"},
        {"id": "Flow_Report_Escalate", "source": "Task_ReportAuthorities", "target": "Task_EscalateAuthorities"},
        {"id": "Flow_Escalate_Initiate", "source": "Task_EscalateAuthorities", "target": "Task_InitiateResponse"},
        {"id": "Flow_Initiate_Assess", "source": "Task_InitiateResponse", "target": "Task_AssessResponseLevel"},
        {"id": "Flow_Assess_Gateway", "source": "Task_AssessResponseLevel", "target": "Gateway_ResponseLevel"},
        {
            "id": "Flow_Response_Internal",
            "source": "Gateway_ResponseLevel",
            "target": "Task_InternalResponse",
            "name": "项目能力充足",
        },
        {
            "id": "Flow_Response_External",
            "source": "Gateway_ResponseLevel",
            "target": "Task_RequestExternalSupport",
            "name": "需外部支援",
        },
        {"id": "Flow_Internal_Merge", "source": "Task_InternalResponse", "target": "Gateway_ResponseMerge"},
        {"id": "Flow_External_Merge", "source": "Task_RequestExternalSupport", "target": "Gateway_ResponseMerge"},
        {"id": "Flow_Merge_Define", "source": "Gateway_ResponseMerge", "target": "Task_DefineTeamMissions"},
        {"id": "Flow_Define_Issue", "source": "Task_DefineTeamMissions", "target": "Task_IssueOrders"},
        {"id": "Flow_Issue_Onsite", "source": "Task_IssueOrders", "target": "Task_OnsiteOperations"},
        {"id": "Flow_Onsite_Split", "source": "Task_OnsiteOperations", "target": "Gateway_ParallelSplit"},
        {"id": "Flow_Split_MedicalPlan", "source": "Gateway_ParallelSplit", "target": "Task_MedicalPlan"},
        {"id": "Flow_MedicalPlan_Aid", "source": "Task_MedicalPlan", "target": "Task_MedicalAid"},
        {"id": "Flow_MedicalAid_Transfer", "source": "Task_MedicalAid", "target": "Task_MedicalTransfer"},
        {"id": "Flow_Medical_Join", "source": "Task_MedicalTransfer", "target": "Gateway_ParallelJoin"},
        {"id": "Flow_Split_EngineeringDeploy", "source": "Gateway_ParallelSplit", "target": "Task_EngineeringDeploy"},
        {"id": "Flow_Engineering_Block", "source": "Task_EngineeringDeploy", "target": "Task_BlockArea"},
        {"id": "Flow_Block_Control", "source": "Task_BlockArea", "target": "Task_EngineeringControl"},
        {"id": "Flow_Engineering_Join", "source": "Task_EngineeringControl", "target": "Gateway_ParallelJoin"},
        {"id": "Flow_Split_Coordination", "source": "Gateway_ParallelSplit", "target": "Task_Coordination"},
        {"id": "Flow_Coordination_Evacuation", "source": "Task_Coordination", "target": "Task_Evacuation"},
        {"id": "Flow_Evacuation_Join", "source": "Task_Evacuation", "target": "Gateway_ParallelJoin"},
        {"id": "Flow_Split_Communications", "source": "Gateway_ParallelSplit", "target": "Task_Communications"},
        {"id": "Flow_Communications_Liaison", "source": "Task_Communications", "target": "Task_ExternalLiaison"},
        {"id": "Flow_Liaison_Join", "source": "Task_ExternalLiaison", "target": "Gateway_ParallelJoin"},
        {"id": "Flow_Join_Recovery", "source": "Gateway_ParallelJoin", "target": "Task_RecoveryTransition"},
        {"id": "Flow_Recovery_Split", "source": "Task_RecoveryTransition", "target": "Gateway_RecoverySplit"},
        {"id": "Flow_RecoverySplit_Aftercare", "source": "Gateway_RecoverySplit", "target": "Task_Aftercare"},
        {"id": "Flow_Aftercare_Psych", "source": "Task_Aftercare", "target": "Task_PsychologicalSupport"},
        {"id": "Flow_Psych_RecoveryMerge", "source": "Task_PsychologicalSupport", "target": "Gateway_RecoveryMerge"},
        {"id": "Flow_RecoverySplit_Investigation", "source": "Gateway_RecoverySplit", "target": "Task_Investigation"},
        {"id": "Flow_Investigation_Collect", "source": "Task_Investigation", "target": "Task_CollectEvidence"},
        {"id": "Flow_Collect_RecoveryMerge", "source": "Task_CollectEvidence", "target": "Gateway_RecoveryMerge"},
        {"id": "Flow_RecoveryMerge_Evaluate", "source": "Gateway_RecoveryMerge", "target": "Task_EvaluateResponse"},
        {"id": "Flow_Evaluate_FinalReport", "source": "Task_EvaluateResponse", "target": "Task_FinalReport"},
        {"id": "Flow_FinalReport_End", "source": "Task_FinalReport", "target": end_id},
    ]

    incoming_map: Dict[str, List[str]] = {spec.element_id: [] for spec in project_nodes}
    outgoing_map: Dict[str, List[str]] = {spec.element_id: [] for spec in project_nodes}
    for flow in sequence_flows:
        outgoing_map[flow["source"]].append(flow["id"])
        incoming_map[flow["target"]].append(flow["id"])

    message_flows = [
        {
            "id": "MessageFlow_GovernmentReport",
            "name": "事故报告",
            "source": "Task_EscalateAuthorities",
            "target": "Task_ReceiveReport",
        },
        {
            "id": "MessageFlow_SupportRequest",
            "name": "支援请求",
            "source": "Task_RequestExternalSupport",
            "target": "Task_ProvideGuidance",
        },
        {
            "id": "MessageFlow_StatusUpdate",
            "name": "现场动态",
            "source": "Task_ExternalLiaison",
            "target": "Task_ProvideGuidance",
        },
    ]

    external_nodes: List[FlowNodeSpec] = [
        FlowNodeSpec(
            element_id="StartEvent_ExternalAlert",
            element_type="bpmn:startEvent",
            name="接收通报",
            lane=None,
            x=2060.0,
            documentation="政府及专业机构接到事故报告，启动协同程序。",
        ),
        FlowNodeSpec(
            element_id="Task_ReceiveReport",
            element_type="bpmn:task",
            name="确认事故信息",
            lane=None,
            x=2220.0,
            documentation="主管部门核实通报内容并登记险情要素。",
        ),
        FlowNodeSpec(
            element_id="Task_ProvideGuidance",
            element_type="bpmn:task",
            name="提供指挥支援",
            lane=None,
            x=4100.0,
            documentation="根据项目请求调配外部力量并反馈处置指令。",
        ),
        FlowNodeSpec(
            element_id="EndEvent_ExternalComplete",
            element_type="bpmn:endEvent",
            name="协同结束",
            lane=None,
            x=4240.0,
        ),
    ]

    external_sequence_flows = [
        {
            "id": "Flow_ExternalStart_Receive",
            "source": "StartEvent_ExternalAlert",
            "target": "Task_ReceiveReport",
        },
        {
            "id": "Flow_ExternalReceive_Support",
            "source": "Task_ReceiveReport",
            "target": "Task_ProvideGuidance",
        },
        {
            "id": "Flow_ExternalSupport_End",
            "source": "Task_ProvideGuidance",
            "target": "EndEvent_ExternalComplete",
        },
    ]

    external_incoming: Dict[str, List[str]] = {spec.element_id: [] for spec in external_nodes}
    external_outgoing: Dict[str, List[str]] = {spec.element_id: [] for spec in external_nodes}
    for flow in external_sequence_flows:
        external_outgoing[flow["source"]].append(flow["id"])
        external_incoming[flow["target"]].append(flow["id"])

    task_data_io: Dict[str, Dict[str, List[Tuple[str, str]]]] = {
        "Task_ReportIncident": {
            "outputs": [("DataAssoc_ReportIncident_Output", "DataRef_IncidentReport")],
        },
        "Task_SummariseSituation": {
            "inputs": [("DataAssoc_Summarise_Input", "DataRef_IncidentReport")],
        },
        "Task_IssueOrders": {
            "outputs": [("DataAssoc_IssueOrders_Output", "DataRef_ResponseOrders")],
        },
        "Task_MedicalPlan": {
            "inputs": [("DataAssoc_MedicalPlan_Input", "DataRef_ResponseOrders")],
        },
        "Task_MedicalAid": {
            "outputs": [("DataAssoc_MedicalAid_Output", "DataRef_MedicalRecord")],
        },
        "Task_EngineeringDeploy": {
            "inputs": [("DataAssoc_EngineeringPlan_Input", "DataRef_ResponseOrders")],
        },
        "Task_EngineeringControl": {
            "outputs": [("DataAssoc_Engineering_Output", "DataRef_EngineeringLog")],
        },
        "Task_Coordination": {
            "inputs": [("DataAssoc_Coordination_Input", "DataRef_ResponseOrders")],
        },
        "Task_Communications": {
            "inputs": [("DataAssoc_Communications_Input", "DataRef_ResponseOrders")],
        },
        "Task_ExternalLiaison": {
            "outputs": [("DataAssoc_StatusUpdates_Output", "DataRef_StatusUpdates")],
        },
        "Task_FinalReport": {
            "inputs": [
                ("DataAssoc_Final_Input_Response", "DataRef_ResponseOrders"),
                ("DataAssoc_Final_Input_Medical", "DataRef_MedicalRecord"),
                ("DataAssoc_Final_Input_Engineering", "DataRef_EngineeringLog"),
                ("DataAssoc_Final_Input_Status", "DataRef_StatusUpdates"),
            ],
            "outputs": [("DataAssoc_Final_Output", "DataRef_FinalReport")],
        },
    }

    lane_height = 140.0
    pool_x = 120.0
    pool_y = 100.0
    pool_width = 5200.0
    project_height = lane_height * len(lanes)
    external_pool_y = pool_y + project_height + 220.0
    external_pool_height = 180.0

    lane_positions: Dict[str, Tuple[float, float, float, float]] = {
        lane.lane_id: (pool_x, pool_y + lane.index * lane_height, pool_width, lane_height)
        for lane in lanes
    }

    element_sizes: Dict[str, Tuple[float, float]] = {
        "bpmn:task": (140.0, 80.0),
        "bpmn:startEvent": (36.0, 36.0),
        "bpmn:endEvent": (36.0, 36.0),
        "bpmn:exclusiveGateway": (50.0, 50.0),
        "bpmn:parallelGateway": (50.0, 50.0),
        "bpmn:intermediateCatchEvent": (36.0, 36.0),
    }

    positions: Dict[str, Tuple[float, float, float, float]] = {}
    for spec in project_nodes:
        width, height = element_sizes.get(spec.element_type, (120.0, 60.0))
        lane = lane_by_name[spec.lane] if spec.lane else None
        if lane is None:
            continue
        lane_bounds = lane_positions[lane.lane_id]
        y = lane_bounds[1] + (lane_bounds[3] - height) / 2.0 + spec.y_offset
        positions[spec.element_id] = (spec.x, y, width, height)

    data_object_size = (60.0, 70.0)
    for data_spec in data_objects:
        lane_bounds = lane_positions[lane_by_name[data_spec.lane].lane_id]
        width, height = data_object_size
        y = lane_bounds[1] + (lane_bounds[3] - height) / 2.0 + data_spec.y_offset
        positions[data_spec.ref_id] = (data_spec.x, y, width, height)

    external_lane_bounds = (pool_x, external_pool_y, pool_width, external_pool_height)
    external_center_y = external_lane_bounds[1] + external_lane_bounds[3] / 2.0
    for spec in external_nodes:
        width, height = element_sizes.get(spec.element_type, (120.0, 60.0))
        y = external_center_y - height / 2.0 + spec.y_offset
        positions[spec.element_id] = (spec.x, y, width, height)

    participant_positions = {
        "Participant_Project": (pool_x, pool_y, pool_width, project_height),
        "Participant_External": (
            external_lane_bounds[0],
            external_lane_bounds[1],
            external_lane_bounds[2],
            external_lane_bounds[3],
        ),
    }

    definitions = ET.Element(
        "bpmn:definitions",
        attrib={
            "id": "Definitions_WaterTrafficEmergency",
            "targetNamespace": "http://example.com/bpmn/water-traffic-emergency",
        },
    )

    collaboration = ET.SubElement(
        definitions,
        "bpmn:collaboration",
        attrib={"id": collaboration_id},
    )
    ET.SubElement(
        collaboration,
        "bpmn:participant",
        attrib={
            "id": "Participant_Project",
            "name": "项目部应急组织",
            "processRef": process_id,
        },
    )
    ET.SubElement(
        collaboration,
        "bpmn:participant",
        attrib={
            "id": "Participant_External",
            "name": "政府及外部支援机构",
            "processRef": external_process_id,
        },
    )
    for message in message_flows:
        ET.SubElement(
            collaboration,
            "bpmn:messageFlow",
            attrib={
                "id": message["id"],
                "name": message["name"],
                "sourceRef": message["source"],
                "targetRef": message["target"],
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

    lane_set = ET.SubElement(process, "bpmn:laneSet", attrib={"id": "LaneSet_Project"})
    for lane in lanes:
        lane_element = ET.SubElement(
            lane_set,
            "bpmn:lane",
            attrib={"id": lane.lane_id, "name": lane.name},
        )
        for flow_node in lane.flow_nodes:
            ET.SubElement(lane_element, "bpmn:flowNodeRef").text = flow_node

    for data_spec in data_objects:
        ET.SubElement(
            process,
            "bpmn:dataObject",
            attrib={"id": data_spec.data_id, "name": data_spec.name},
        )
        ET.SubElement(
            process,
            "bpmn:dataObjectReference",
            attrib={
                "id": data_spec.ref_id,
                "dataObjectRef": data_spec.data_id,
                "name": data_spec.name,
            },
        )

    data_association_edges: List[Tuple[str, str, str, str]] = []
    for spec in project_nodes:
        element = ET.SubElement(
            process,
            spec.element_type,
            attrib={"id": spec.element_id, "name": spec.name},
        )
        for flow_id in incoming_map.get(spec.element_id, []):
            ET.SubElement(element, "bpmn:incoming").text = flow_id
        for flow_id in outgoing_map.get(spec.element_id, []):
            ET.SubElement(element, "bpmn:outgoing").text = flow_id
        documentation = spec.documentation or (spec.task.documentation if spec.task else "")
        if documentation:
            doc = ET.SubElement(element, "bpmn:documentation")
            doc.text = documentation
        if spec.element_type == "bpmn:task" and spec.element_id in task_data_io:
            io_config = task_data_io[spec.element_id]
            inputs = io_config.get("inputs", [])
            outputs = io_config.get("outputs", [])
            io_spec = ET.SubElement(element, "bpmn:ioSpecification")
            input_ids: List[str] = []
            output_ids: List[str] = []
            for index, _ in enumerate(inputs, start=1):
                data_input_id = f"{spec.element_id}_Input_{index}"
                ET.SubElement(io_spec, "bpmn:dataInput", attrib={"id": data_input_id})
                input_ids.append(data_input_id)
            for index, _ in enumerate(outputs, start=1):
                data_output_id = f"{spec.element_id}_Output_{index}"
                ET.SubElement(io_spec, "bpmn:dataOutput", attrib={"id": data_output_id})
                output_ids.append(data_output_id)
            if input_ids:
                input_set = ET.SubElement(io_spec, "bpmn:inputSet")
                for data_input_id in input_ids:
                    ET.SubElement(input_set, "bpmn:dataInputRefs").text = data_input_id
            if output_ids:
                output_set = ET.SubElement(io_spec, "bpmn:outputSet")
                for data_output_id in output_ids:
                    ET.SubElement(output_set, "bpmn:dataOutputRefs").text = data_output_id
            for (assoc_id, data_ref), data_input_id in zip(inputs, input_ids):
                assoc = ET.SubElement(
                    element,
                    "bpmn:dataInputAssociation",
                    attrib={"id": assoc_id},
                )
                ET.SubElement(assoc, "bpmn:sourceRef").text = data_ref
                ET.SubElement(assoc, "bpmn:targetRef").text = data_input_id
                data_association_edges.append(("input", assoc_id, data_ref, spec.element_id))
            for (assoc_id, data_ref), data_output_id in zip(outputs, output_ids):
                assoc = ET.SubElement(
                    element,
                    "bpmn:dataOutputAssociation",
                    attrib={"id": assoc_id},
                )
                ET.SubElement(assoc, "bpmn:sourceRef").text = data_output_id
                ET.SubElement(assoc, "bpmn:targetRef").text = data_ref
                data_association_edges.append(("output", assoc_id, spec.element_id, data_ref))

    for flow in sequence_flows:
        attributes = {
            "id": flow["id"],
            "sourceRef": flow["source"],
            "targetRef": flow["target"],
        }
        if flow.get("name"):
            attributes["name"] = flow["name"]
        ET.SubElement(process, "bpmn:sequenceFlow", attrib=attributes)

    external_process = ET.SubElement(
        definitions,
        "bpmn:process",
        attrib={
            "id": external_process_id,
            "name": "外部支援协同",
            "isExecutable": "false",
        },
    )

    for spec in external_nodes:
        element = ET.SubElement(
            external_process,
            spec.element_type,
            attrib={"id": spec.element_id, "name": spec.name},
        )
        for flow_id in external_incoming.get(spec.element_id, []):
            ET.SubElement(element, "bpmn:incoming").text = flow_id
        for flow_id in external_outgoing.get(spec.element_id, []):
            ET.SubElement(element, "bpmn:outgoing").text = flow_id
        if spec.documentation:
            doc = ET.SubElement(element, "bpmn:documentation")
            doc.text = spec.documentation

    for flow in external_sequence_flows:
        ET.SubElement(
            external_process,
            "bpmn:sequenceFlow",
            attrib={"id": flow["id"], "sourceRef": flow["source"], "targetRef": flow["target"]},
        )

    diagram = ET.SubElement(
        definitions,
        "bpmndi:BPMNDiagram",
        attrib={"id": "BPMNDiagram_WaterTrafficEmergency"},
    )
    plane = ET.SubElement(
        diagram,
        "bpmndi:BPMNPlane",
        attrib={"id": "BPMNPlane_WaterTrafficEmergency", "bpmnElement": collaboration_id},
    )

    for participant_id, bounds in participant_positions.items():
        shape = ET.SubElement(
            plane,
            "bpmndi:BPMNShape",
            attrib={
                "id": f"{participant_id}_di",
                "bpmnElement": participant_id,
                "isHorizontal": "true",
            },
        )
        ET.SubElement(
            shape,
            "dc:Bounds",
            attrib={
                "x": f"{bounds[0]:.2f}",
                "y": f"{bounds[1]:.2f}",
                "width": f"{bounds[2]:.2f}",
                "height": f"{bounds[3]:.2f}",
            },
        )

    for lane in lanes:
        lane_bounds = lane_positions[lane.lane_id]
        lane_shape = ET.SubElement(
            plane,
            "bpmndi:BPMNShape",
            attrib={
                "id": f"{lane.lane_id}_di",
                "bpmnElement": lane.lane_id,
                "isHorizontal": "true",
            },
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

    for flow in sequence_flows + external_sequence_flows:
        edge = ET.SubElement(
            plane,
            "bpmndi:BPMNEdge",
            attrib={"id": f"{flow['id']}_di", "bpmnElement": flow["id"]},
        )
        for point in connection_points(positions[flow["source"]], positions[flow["target"]]):
            ET.SubElement(
                edge,
                "di:waypoint",
                attrib={"x": f"{point[0]:.2f}", "y": f"{point[1]:.2f}"},
            )

    for message in message_flows:
        edge = ET.SubElement(
            plane,
            "bpmndi:BPMNEdge",
            attrib={"id": f"{message['id']}_di", "bpmnElement": message["id"]},
        )
        for point in connection_points(positions[message["source"]], positions[message["target"]]):
            ET.SubElement(
                edge,
                "di:waypoint",
                attrib={"x": f"{point[0]:.2f}", "y": f"{point[1]:.2f}"},
            )

    for assoc_type, assoc_id, source_id, target_id in data_association_edges:
        edge = ET.SubElement(
            plane,
            "bpmndi:BPMNEdge",
            attrib={"id": f"{assoc_id}_di", "bpmnElement": assoc_id},
        )
        if assoc_type == "input":
            start_bounds = positions[source_id]
            end_bounds = positions[target_id]
        else:
            start_bounds = positions[source_id]
            end_bounds = positions[target_id]
        for point in connection_points(start_bounds, end_bounds):
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
