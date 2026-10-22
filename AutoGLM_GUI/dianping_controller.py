"""Bounded, observation-driven navigation for Dianping's city/brand search.

The caller owns fresh UI/screenshot acquisition, device leases and action execution.
Returning ``finish`` only requests the existing independent final verification.
No action receipt is accepted as evidence that a transition has completed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

PACKAGE = "com.dianping.v1"
_PREFIX = f"{PACKAGE}:id/"
_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_IME_PACKAGES = frozenset(
    {
        "com.android.adbkeyboard",
        "com.android.inputmethod.latin",
        "com.google.android.inputmethod.latin",
        "com.baidu.input",
        "com.baidu.input_oppo",
        "com.sohu.inputmethod.sogou",
        "com.sohu.inputmethod.sogouoem",
        "com.iflytek.inputmethod",
    }
)


class DianpingConditionError(RuntimeError):
    """The current observation cannot authorize a bounded skill action."""


@dataclass(frozen=True)
class Decision:
    action: dict[str, Any]
    phase: str
    reason: str


class _Tree:
    def __init__(self, xml: str, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise DianpingConditionError("屏幕尺寸无效")
        if len(xml) > 2_000_000 or "<!DOCTYPE" in xml or "<!ENTITY" in xml:
            raise DianpingConditionError("界面层级格式不受支持")
        try:
            self.root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise DianpingConditionError("界面层级无法解析") from exc
        if self.root.tag != "hierarchy":
            raise DianpingConditionError("缺少界面层级根节点")
        self.width, self.height = width, height
        self.parents = {
            child: parent for parent in self.root.iter() for child in parent
        }
        raw = list(self.root.iter("node"))
        if not raw or len(raw) > 10_000:
            raise DianpingConditionError("界面层级为空或超出限制")
        self.nodes = [node for node in raw if self.visible(node)]
        if not any(node.get("package") == PACKAGE for node in self.nodes):
            raise DianpingConditionError("当前界面不是大众点评")
        windows = [node for node in self.root if node.get("package") == PACKAGE]
        if len(windows) != 1 or self.bounds(windows[0]) != (0, 0, width, height):
            raise DianpingConditionError("界面窗口不唯一或与屏幕几何不一致")
        self._check_overlays()

    def bounds(self, node: ET.Element) -> tuple[int, int, int, int] | None:
        match = _BOUNDS.fullmatch(node.get("bounds", ""))
        if not match:
            return None
        left, top, right, bottom = map(int, match.groups())
        if not (0 <= left < right <= self.width and 0 <= top < bottom <= self.height):
            return None
        return left, top, right, bottom

    def visible(self, node: ET.Element) -> bool:
        bounds = self.bounds(node)
        if node.get("enabled") != "true" or bounds is None:
            return False
        left, top, right, bottom = bounds
        current: ET.Element | None = node
        while current is not None and current.tag == "node":
            if current.get("visible-to-user", "true") != "true":
                return False
            parent_bounds = self.bounds(current)
            if parent_bounds is None:
                return False
            left, top = max(left, parent_bounds[0]), max(top, parent_bounds[1])
            right, bottom = min(right, parent_bounds[2]), min(bottom, parent_bounds[3])
            if left >= right or top >= bottom:
                return False
            current = self.parents.get(current)
        return True

    def find(
        self,
        *,
        resource: str | None = None,
        text: str | None = None,
        desc: str | None = None,
        within: ET.Element | None = None,
    ) -> list[ET.Element]:
        candidates = self.nodes if within is None else within.iter("node")
        return [
            node
            for node in candidates
            if node.get("package") == PACKAGE
            and self.visible(node)
            and (resource is None or node.get("resource-id") == _PREFIX + resource)
            and (text is None or node.get("text") == text)
            and (desc is None or node.get("content-desc") == desc)
        ]

    @staticmethod
    def one(nodes: list[ET.Element], label: str) -> ET.Element:
        if len(nodes) != 1:
            raise DianpingConditionError(f"{label}缺失或不唯一")
        return nodes[0]

    def _check_overlays(self) -> None:
        editable = self.find(resource="search_keyword_edit")
        keyboard_allowed = len(editable) == 1 and editable[0].get("focused") == "true"
        for node in self.nodes:
            package = node.get("package", "")
            if package == PACKAGE:
                if node.get("class", "").endswith("Dialog") or node.get(
                    "resource-id"
                ) in {"android:id/parentPanel", "android:id/alertTitle"}:
                    raise DianpingConditionError("检测到对话框，停止自动操作")
                continue
            bounds = self.bounds(node)
            assert bounds is not None
            _, top, _, bottom = bounds
            if (
                package == "com.android.systemui"
                and bottom - top <= self.height * 0.08
                and (bottom <= self.height * 0.08 or top >= self.height * 0.92)
            ):
                continue
            if keyboard_allowed and package in _IME_PACKAGES:
                edit_bounds = self.bounds(editable[0])
                assert edit_bounds is not None
                if top >= edit_bounds[3]:
                    continue
            raise DianpingConditionError("存在非目标应用覆盖，停止自动操作")

    def tap(self, node: ET.Element) -> dict[str, Any]:
        bounds = self.bounds(node)
        if not self.visible(node) or node.get("package") != PACKAGE or bounds is None:
            raise DianpingConditionError("点击目标无效")
        # Text labels often delegate clicks to a nearby ancestor. Keep the point
        # inside the observed label, never use the center of a whole-page parent.
        current = node
        for _ in range(5):
            if current.get("enabled") != "true":
                break
            if current.get("clickable") == "true":
                break
            parent = self.parents.get(current)
            if parent is None:
                break
            current = parent
        else:
            raise DianpingConditionError("目标缺少可点击容器")
        if current.get("clickable") != "true" or not self.visible(current):
            raise DianpingConditionError("目标不可点击")
        x = (bounds[0] + bounds[2]) / 2
        y = (bounds[1] + bounds[3]) / 2
        ancestor = self.parents.get(node)
        while ancestor is not None and ancestor.tag == "node":
            ancestor_bounds = self.bounds(ancestor)
            if ancestor_bounds is None or not (
                ancestor_bounds[0] <= x < ancestor_bounds[2]
                and ancestor_bounds[1] <= y < ancestor_bounds[3]
            ):
                raise DianpingConditionError("点击中心被父容器裁剪")
            ancestor = self.parents.get(ancestor)
        target_bounds = self.bounds(current)
        assert target_bounds is not None
        if not (
            target_bounds[0] <= x < target_bounds[2]
            and target_bounds[1] <= y < target_bounds[3]
        ):
            raise DianpingConditionError("目标与点击容器几何不一致")
        for overlay in self.find(desc="floatingWindow"):
            overlay_bounds = self.bounds(overlay)
            assert overlay_bounds is not None
            if (
                overlay_bounds[0] <= x < overlay_bounds[2]
                and overlay_bounds[1] <= y < overlay_bounds[3]
            ):
                raise DianpingConditionError("点击目标被浮层遮挡")
        point = [round(x * 1000 / self.width), round(y * 1000 / self.height)]
        # Quantization must not move the actual touch outside a narrow target.
        if not (
            bounds[0] <= point[0] * self.width / 1000 < bounds[2]
            and bounds[1] <= point[1] * self.height / 1000 < bounds[3]
        ):
            raise DianpingConditionError("目标过小，无法安全转换坐标")
        return {"_metadata": "do", "action": "Tap", "element": point}


class DianpingController:
    """One task, one controller; only observed transitions advance the task.

    Supported city choices are visible exact matches in history or popular cities.
    Unknown layouts and unavailable cities stop without a model/write fallback.
    Instances must not be reused after cancellation, disconnect or unknown actions.
    """

    def __init__(self, city: str, shop_name: str) -> None:
        if (
            not city
            or not shop_name
            or city != city.strip()
            or shop_name != shop_name.strip()
        ):
            raise ValueError("city and shop_name must be non-empty exact values")
        self.city = city
        self.shop_name = shop_name
        self._city_observed = False
        self._typed = False
        self._submitted = False
        self._back_count = 0
        self._issued: set[str] = set()
        self._waits: dict[str, int] = {}

    def decide(self, xml: str, width: int, height: int) -> Decision:
        tree = _Tree(xml, width, height)
        result = tree.find(resource="search_keyword")
        edit = tree.find(resource="search_keyword_edit")
        home = tree.find(resource="home_tab_home_container")
        city_panel = bool(tree.find(text="搜索城市/区县") and tree.find(text="境内"))
        scenes = sum((bool(result), bool(edit), bool(home), city_panel))
        if scenes != 1:
            raise DianpingConditionError("页面未知或页面标志冲突，停止自动操作")
        if result:
            return self._results(tree, tree.one(result, "搜索结果关键词"))
        if edit:
            return self._input(tree, tree.one(edit, "关键词输入框"))
        if city_panel:
            return self._city_panel(tree)
        return self._home(tree)

    def _once(
        self, key: str, action: dict[str, Any], phase: str, reason: str
    ) -> Decision:
        if key in self._issued:
            return self._wait(key, phase, "等待已发出的导航动作生效")
        self._issued.add(key)
        return Decision(action, phase, reason)

    def _wait(self, key: str, phase: str, reason: str) -> Decision:
        count = self._waits.get(key, 0)
        if count >= 2:
            raise DianpingConditionError(f"{reason}超时，不重复操作")
        self._waits[key] = count + 1
        return Decision(
            {"_metadata": "do", "action": "Wait", "duration": "0.5 seconds"},
            phase,
            reason,
        )

    def _back(self) -> Decision:
        if self._city_observed or self._typed or self._submitted:
            raise DianpingConditionError("流程意外返回旧页面，不自动重试")
        if self._back_count >= 3:
            raise DianpingConditionError("返回首页已达三次上限")
        self._back_count += 1
        return Decision(
            {"_metadata": "do", "action": "Back"},
            "return_home",
            "返回已识别的搜索页面，直到首页可见",
        )

    def _home(self, tree: _Tree) -> Decision:
        tree.one(tree.find(resource="home_tab_home_container"), "首页入口")
        search = tree.one(tree.find(resource="title_search_bar"), "首页搜索栏")
        city_nodes: list[ET.Element] = []
        for tab in tree.find(resource="cl_tab"):
            if tree.find(resource="tab_arrow", within=tab):
                city_nodes.extend(tree.find(resource="tab_text", within=tab))
        city_node = tree.one(city_nodes, "首页城市")
        if self._typed or self._submitted:
            raise DianpingConditionError("输入或提交后意外返回首页，不重试搜索")
        if city_node.get("text") != self.city:
            if self._city_observed:
                raise DianpingConditionError("已确认的城市发生变化")
            if "select_city" in self._issued:
                return self._wait(
                    "city_confirmation", "select_city", "等待首页显示准确城市"
                )
            return self._once(
                "open_city",
                tree.tap(city_node),
                "select_city",
                "首页城市与目标不符，打开城市列表",
            )
        self._city_observed = True
        entries = [
            node
            for node in tree.find(within=search)
            if node.get("class") == "android.view.View"
            and node.get("clickable") == "true"
        ]
        entry = tree.one(entries, "首页搜索输入入口")
        return self._once(
            "open_search",
            tree.tap(entry),
            "enter_keyword",
            "已观察到准确城市，打开搜索输入框",
        )

    def _city_panel(self, tree: _Tree) -> Decision:
        if self._city_observed or self._typed or self._submitted:
            raise DianpingConditionError("城市确认后意外进入城市列表")
        if "select_city" in self._issued:
            return self._wait("select_city", "select_city", "等待已选择城市返回首页")
        for heading in ("当前定位/历史访问", "热门城市"):
            headers = tree.find(text=heading)
            if not headers:
                continue
            header = tree.one(headers, "城市区段标题")
            parent = tree.parents.get(header)
            section = tree.parents.get(parent) if parent is not None else None
            if section is None:
                continue
            matches = tree.find(text=self.city, within=section)
            if not matches:
                continue
            target = tree.one(matches, f"{heading}中的目标城市")
            return self._once(
                "select_city",
                tree.tap(target),
                "select_city",
                f"选择{heading}中全文匹配的城市",
            )
        raise DianpingConditionError("目标城市不在已支持的可见城市区段")

    def _input(self, tree: _Tree, edit: ET.Element) -> Decision:
        if (
            edit.get("class") != "android.widget.EditText"
            or edit.get("password") == "true"
        ):
            raise DianpingConditionError("关键词控件不是可编辑的普通输入框")
        if not self._city_observed:
            return self._back()
        if self._submitted:
            return self._wait(
                "submit_search", "show_merchants", "等待已提交搜索进入结果页"
            )
        if edit.get("text", "") == self.shop_name:
            submit = tree.one(
                tree.find(resource="search_button", text="搜索"), "搜索提交按钮"
            )
            action = tree.tap(submit)
            self._submitted = True
            return Decision(
                action, "submit_search", "输入框全文与目标一致，仅提交这一次搜索"
            )
        if self._typed:
            raise DianpingConditionError("输入后全文不匹配，不重复输入或提交")
        if edit.get("focused") != "true":
            return self._once(
                "focus_keyword",
                tree.tap(edit),
                "enter_keyword",
                "聚焦准确的关键词输入框",
            )
        self._typed = True
        return Decision(
            {"_metadata": "do", "action": "Type", "text": self.shop_name},
            "enter_keyword",
            "输入完整关键词；历史词和占位提示不作为输入证据",
        )

    def _branch_title(self, node: ET.Element) -> bool:
        text = node.get("text", "")
        suffix = text[len(self.shop_name) :] if text.startswith(self.shop_name) else ""
        return bool(re.match(r"\s*[（(].*店[）)]$", suffix))

    def _complete_results(self, tree: _Tree, merchants: ET.Element) -> bool:
        titles = [
            node for node in tree.find(within=merchants) if self._branch_title(node)
        ]
        names: set[str] = set()
        for title in titles:
            current = tree.parents.get(title)
            while current is not None and current is not merchants:
                local_titles = [
                    node
                    for node in tree.find(within=current)
                    if self._branch_title(node)
                ]
                if len(local_titles) > 1:
                    break
                if tree.find(text=self.city, within=current):
                    names.add(title.get("text", ""))
                    break
                current = tree.parents.get(current)
        return len(names) >= 2

    def _results(self, tree: _Tree, query: ET.Element) -> Decision:
        if query.get("text") != self.shop_name:
            if self._submitted:
                raise DianpingConditionError("提交后结果关键词不匹配，不重新搜索")
            return self._back()
        merchants = tree.find(desc="SearchPagerListView-商户")
        if merchants:
            visible_lists = [
                node
                for node in tree.find()
                if node.get("content-desc", "").startswith("SearchPagerListView-")
            ]
            if len(visible_lists) != 1:
                raise DianpingConditionError(
                    "多个结果分类同时可见，无法证明商户分类已选中"
                )
            listing = tree.one(merchants, "商户结果列表")
            if self._complete_results(tree, listing):
                return Decision(
                    {
                        "_metadata": "finish",
                        "message": "城市、完整关键词和多个商户已可见，申请最终独立验收",
                    },
                    "final_verification",
                    "新观察显示正确关键词、商户列表及至少两个目标城市门店；完成状态仍由独立验收决定",
                )
            if not self._submitted:
                return self._back()
            return self._wait(
                "merchant_results",
                "show_merchants",
                "等待至少两个明确属于目标城市的商户",
            )
        if not self._city_observed or not self._submitted:
            return self._back()
        tab = tree.one(tree.find(text="商户"), "商户分类")
        return self._once(
            "select_merchants",
            tree.tap(tab),
            "show_merchants",
            "关键词已匹配，选择商户分类",
        )
