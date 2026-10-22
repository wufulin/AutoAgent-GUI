"""Behavior and fail-closed boundaries for the conditional Dianping skill."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from AutoGLM_GUI.dianping_controller import (
    PACKAGE,
    DianpingConditionError,
    DianpingController,
)


def node(*children: ET.Element, **attrs: str) -> ET.Element:
    defaults = {
        "package": PACKAGE,
        "class": "android.widget.FrameLayout",
        "enabled": "true",
        "clickable": "false",
        "focused": "false",
        "selected": "false",
        "text": "",
        "bounds": "[0,0][1080,2412]",
    }
    aliases = {"resource": "resource-id", "desc": "content-desc", "cls": "class"}
    for key, value in attrs.items():
        defaults[aliases.get(key, key)] = (
            f"{PACKAGE}:id/{value}" if key == "resource" else value
        )
    result = ET.Element("node", defaults)
    result.extend(children)
    return result


def xml(*children: ET.Element, external: ET.Element | None = None) -> str:
    root = ET.Element("hierarchy", rotation="0")
    root.append(node(*children))
    if external is not None:
        root.append(external)
    return ET.tostring(root, encoding="unicode")


def home(city: str = "广州") -> str:
    return xml(
        node(resource="home_tab_home_container", selected="true"),
        node(
            node(resource="tab_text", text="关注"),
            resource="cl_tab",
        ),
        node(
            node(resource="tab_text", text=city, bounds="[170,110][270,210]"),
            node(resource="tab_arrow", clickable="true", bounds="[280,150][315,170]"),
            resource="cl_tab",
            clickable="true",
            bounds="[160,100][330,220]",
        ),
        node(
            node(
                cls="android.view.View", clickable="true", bounds="[130,230][780,320]"
            ),
            node(text="搜索", clickable="true", bounds="[880,230][1050,320]"),
            resource="title_search_bar",
        ),
    )


def city_panel(*, history: tuple[str, ...] = ("深圳", "广州")) -> str:
    def section(heading: str, cities: tuple[str, ...], top: int) -> ET.Element:
        return node(
            node(node(text=heading)),
            node(
                *[
                    node(
                        node(
                            text=city,
                            bounds=f"[{100 + i * 180},{top}][{200 + i * 180},{top + 60}]",
                        ),
                        clickable="true",
                    )
                    for i, city in enumerate(cities)
                ]
            ),
        )

    return xml(
        node(text="搜索城市/区县"),
        node(text="境内"),
        section("当前定位/历史访问", history, 500),
        section("热门城市", ("广州", "深圳", "北京"), 1000),
    )


def input_page(text: str = "", *, focused: bool = True) -> str:
    return xml(
        node(
            resource="search_keyword_edit",
            cls="android.widget.EditText",
            text=text,
            focused=str(focused).lower(),
            clickable="true",
            bounds="[170,130][800,230]",
        ),
        node(resource="search_hint", text="霸王茶姬"),
        node(
            resource="search_button",
            text="搜索",
            clickable="true",
            bounds="[870,130][1040,230]",
        ),
        node(text="古茗", clickable="true", bounds="[100,400][300,450]"),
    )


def results(
    *,
    query: str = "霸王茶姬",
    merchant: bool = True,
    cities: tuple[str, ...] = ("广州", "广州"),
    names: tuple[str, ...] | None = None,
) -> str:
    if names is None:
        names = tuple(f"{query}({i}号店)" for i in range(len(cities)))
    cards = [
        node(
            node(text=name, bounds=f"[330,{550 + i * 600}][900,{610 + i * 600}]"),
            node(text=city, bounds=f"[970,{700 + i * 600}][1050,{750 + i * 600}]"),
            clickable="true",
        )
        for i, (name, city) in enumerate(zip(names, cities, strict=True))
    ]
    return xml(
        node(resource="search_keyword", text=query, bounds="[170,130][820,230]"),
        node(text="全部", clickable="true", bounds="[40,270][140,330]"),
        node(text="商户", clickable="true", bounds="[230,270][330,330]"),
        node(
            *cards,
            desc="SearchPagerListView-商户" if merchant else "SearchPagerListView-全部",
        ),
    )


def decide(controller: DianpingController, observation: str):
    return controller.decide(observation, 1080, 2412)


def submitted_controller(
    city: str = "广州", shop: str = "霸王茶姬"
) -> DianpingController:
    controller = DianpingController(city, shop)
    assert decide(controller, home(city)).phase == "enter_keyword"
    assert decide(controller, input_page()).action["action"] == "Type"
    assert decide(controller, input_page(shop)).phase == "submit_search"
    return controller


def test_observed_city_input_and_merchant_flow_requests_independent_verification():
    controller = DianpingController("广州", "霸王茶姬")
    assert (
        decide(controller, results(query="其他", cities=("上海", "上海"))).action[
            "action"
        ]
        == "Back"
    )
    assert decide(controller, input_page("其他")).action["action"] == "Back"
    assert decide(controller, home("上海")).phase == "select_city"
    history_choice = decide(controller, city_panel())
    assert history_choice.action["action"] == "Tap"
    assert history_choice.action["element"][1] < 300  # history, not popular duplicate
    assert decide(controller, home()).phase == "enter_keyword"
    assert decide(controller, input_page()).action == {
        "_metadata": "do",
        "action": "Type",
        "text": "霸王茶姬",
    }
    assert decide(controller, input_page("霸王茶姬")).phase == "submit_search"
    assert decide(controller, results(merchant=False)).action["action"] == "Tap"
    final = decide(controller, results())
    assert final.action["_metadata"] == "finish"
    assert final.phase == "final_verification"
    assert "独立验收" in final.action["message"]


def test_already_complete_results_can_request_verification_without_writes():
    final = decide(
        DianpingController("深圳", "古茗"),
        results(query="古茗", cities=("深圳", "深圳")),
    )
    assert final.action["_metadata"] == "finish"


def test_home_city_is_identified_by_arrow_relation_not_generic_tab_or_substring():
    controller = DianpingController("广州", "霸王茶姬")
    assert decide(controller, home("广州市")).phase == "select_city"
    assert decide(controller, input_page("霸王茶姬")).action["action"] == "Back"


def test_city_selection_does_not_confirm_city_until_home_observation():
    controller = DianpingController("广州", "霸王茶姬")
    decide(controller, city_panel())
    assert decide(controller, input_page("霸王茶姬")).action["action"] == "Back"


def test_city_choice_uses_popular_section_when_history_has_no_exact_match():
    choice = decide(
        DianpingController("广州", "霸王茶姬"), city_panel(history=("广州市",))
    )
    assert choice.action["element"][1] > 400


def test_duplicate_city_within_same_section_stops():
    with pytest.raises(DianpingConditionError, match="不唯一"):
        decide(
            DianpingController("广州", "霸王茶姬"), city_panel(history=("广州", "广州"))
        )


def test_invisible_city_is_not_searched_or_guessed():
    with pytest.raises(DianpingConditionError, match="可见城市"):
        decide(DianpingController("杭州", "霸王茶姬"), city_panel())


def test_back_to_home_is_limited_to_three_observed_search_screens():
    controller = DianpingController("广州", "霸王茶姬")
    for _ in range(3):
        assert decide(controller, input_page("其他")).action["action"] == "Back"
    with pytest.raises(DianpingConditionError, match="三次"):
        decide(controller, input_page("其他"))


def test_placeholder_and_history_do_not_replace_actual_input():
    controller = DianpingController("深圳", "古茗")
    decide(controller, home("深圳"))
    action = decide(controller, input_page()).action
    assert action == {"_metadata": "do", "action": "Type", "text": "古茗"}


def test_input_must_be_focused_before_typing_and_is_never_replayed():
    controller = DianpingController("广州", "霸王茶姬")
    decide(controller, home())
    assert decide(controller, input_page(focused=False)).action["action"] == "Tap"
    assert decide(controller, input_page()).action["action"] == "Type"
    with pytest.raises(DianpingConditionError, match="全文不匹配"):
        decide(controller, input_page("霸王"))


def test_exact_prefilled_input_can_submit_but_never_twice():
    controller = DianpingController("广州", "霸王茶姬")
    decide(controller, home())
    assert decide(controller, input_page("霸王茶姬")).phase == "submit_search"
    for _ in range(2):
        assert decide(controller, input_page("霸王茶姬")).action["action"] == "Wait"
    with pytest.raises(DianpingConditionError, match="不重复操作"):
        decide(controller, input_page("霸王茶姬"))


def test_navigation_is_not_blindly_replayed():
    controller = DianpingController("广州", "霸王茶姬")
    assert decide(controller, home()).action["action"] == "Tap"
    for _ in range(2):
        assert decide(controller, home()).action["action"] == "Wait"
    with pytest.raises(DianpingConditionError):
        decide(controller, home())


def test_all_results_tab_cannot_finish_even_with_correct_brand_city_and_shops():
    controller = submitted_controller()
    decision = decide(controller, results(merchant=False))
    assert decision.action["action"] == "Tap"
    assert decision.phase == "show_merchants"


@pytest.mark.parametrize(
    "hidden", ["ancestor", "offscreen", "zero_area", "outside_parent"]
)
def test_cached_hidden_merchant_page_cannot_prove_selected_tab(hidden: str):
    all_root = ET.fromstring(results(merchant=False))
    merchants_root = ET.fromstring(results())
    merchants = next(
        n
        for n in merchants_root.iter("node")
        if n.get("content-desc") == "SearchPagerListView-商户"
    )
    wrapper = node(merchants)
    if hidden == "ancestor":
        wrapper.set("visible-to-user", "false")
    elif hidden == "offscreen":
        merchants.set("bounds", "[1080,360][2160,2364]")
    elif hidden == "zero_area":
        merchants.set("bounds", "[0,360][0,2364]")
    else:
        wrapper.set("bounds", "[0,0][1080,100]")
        merchants.set("bounds", "[0,360][1080,2364]")
    all_root[0].append(wrapper)
    decision = decide(submitted_controller(), ET.tostring(all_root, encoding="unicode"))
    assert decision.action["action"] == "Tap"
    assert decision.phase == "show_merchants"


def test_conflicting_visible_result_pages_cannot_finish():
    root = ET.fromstring(results())
    root[0].append(node(desc="SearchPagerListView-全部", bounds="[0,360][1080,2364]"))
    with pytest.raises(DianpingConditionError, match="多个结果分类"):
        decide(submitted_controller(), ET.tostring(root, encoding="unicode"))


def test_hidden_shop_cards_cannot_supply_completion_evidence():
    root = ET.fromstring(results())
    listing = next(
        n
        for n in root.iter("node")
        if n.get("content-desc") == "SearchPagerListView-商户"
    )
    listing[1].set("visible-to-user", "false")
    decision = decide(submitted_controller(), ET.tostring(root, encoding="unicode"))
    assert decision.action["action"] == "Wait"


def test_disabled_layout_wrapper_does_not_hide_enabled_children():
    # Actual Dianping result headers contain an enabled child under a disabled
    # decorative wrapper. Visibility and enabled controls are distinct signals.
    root = ET.fromstring(results())
    query = next(
        n
        for n in root.iter("node")
        if n.get("resource-id", "").endswith("/search_keyword")
    )
    root[0].remove(query)
    root[0].append(node(query, enabled="false"))
    decision = decide(submitted_controller(), ET.tostring(root, encoding="unicode"))
    assert decision.action["_metadata"] == "finish"


@pytest.mark.parametrize(
    "observation",
    [
        results(cities=("广州市", "广州")),
        results(cities=("深圳", "广州")),
        results(cities=("广州",)),
        results(names=("霸王茶姬(同一家店)", "霸王茶姬(同一家店)")),
        results(names=("霸王茶姬（大杯）", "霸王茶姬（小杯）")),
    ],
)
def test_incomplete_or_non_merchant_results_never_finish(observation: str):
    controller = submitted_controller()
    assert decide(controller, observation).action["action"] == "Wait"
    assert decide(controller, observation).action["action"] == "Wait"
    with pytest.raises(DianpingConditionError):
        decide(controller, observation)


def test_unrelated_city_label_does_not_prove_each_shop_city():
    root = ET.fromstring(results(cities=("深圳", "深圳")))
    listing = next(
        n
        for n in root.iter("node")
        if n.get("content-desc") == "SearchPagerListView-商户"
    )
    listing.append(node(text="广州"))
    assert (
        decide(submitted_controller(), ET.tostring(root, encoding="unicode")).action[
            "action"
        ]
        == "Wait"
    )


def test_query_change_after_submission_stops_without_recovery():
    with pytest.raises(DianpingConditionError, match="不重新搜索"):
        decide(submitted_controller(), results(query="霸王茶姬旗舰店"))


def test_unexpected_home_after_submission_stops_without_new_search():
    with pytest.raises(DianpingConditionError, match="不重试搜索"):
        decide(submitted_controller(), home())


@pytest.mark.parametrize(
    "bounds", ["[0,0][0,2]", "[0,0][1081,230]", "[-1,0][10,10]", "bad"]
)
def test_invalid_target_bounds_are_not_used(bounds: str):
    root = ET.fromstring(home())
    target = next(n for n in root.iter("node") if n.get("class") == "android.view.View")
    target.set("bounds", bounds)
    with pytest.raises(DianpingConditionError):
        decide(
            DianpingController("广州", "霸王茶姬"),
            ET.tostring(root, encoding="unicode"),
        )


def test_disabled_and_ambiguous_targets_are_rejected():
    for mode in ("disabled", "duplicate"):
        root = ET.fromstring(home())
        target = next(
            n for n in root.iter("node") if n.get("class") == "android.view.View"
        )
        if mode == "disabled":
            target.set("enabled", "false")
        else:
            root[0].append(
                node(*[node(**dict(target.attrib))], resource="title_search_bar")
            )
        with pytest.raises(DianpingConditionError):
            decide(
                DianpingController("广州", "霸王茶姬"),
                ET.tostring(root, encoding="unicode"),
            )


@pytest.mark.parametrize(
    "package",
    ["com.other.app", "com.android.permissioncontroller", "com.android.systemui"],
)
def test_external_fullscreen_overlay_stops(package: str):
    root = ET.fromstring(home())
    root.append(node(package=package, text="允许", clickable="true"))
    with pytest.raises(DianpingConditionError, match="覆盖"):
        decide(
            DianpingController("广州", "霸王茶姬"),
            ET.tostring(root, encoding="unicode"),
        )


def test_app_dialog_stops_before_operating_underlying_controls():
    root = ET.fromstring(home())
    root[0].append(node(**{"resource-id": "android:id/parentPanel"}))
    with pytest.raises(DianpingConditionError, match="对话框"):
        decide(
            DianpingController("广州", "霸王茶姬"),
            ET.tostring(root, encoding="unicode"),
        )


def test_known_keyboard_is_allowed_only_below_focused_input():
    controller = DianpingController("广州", "霸王茶姬")
    decide(controller, home())
    root = ET.fromstring(input_page())
    root.append(node(package="com.android.adbkeyboard", bounds="[0,1500][1080,2412]"))
    assert (
        decide(controller, ET.tostring(root, encoding="unicode")).action["action"]
        == "Type"
    )
    root[-1].set("bounds", "[0,100][1080,2412]")
    with pytest.raises(DianpingConditionError, match="覆盖"):
        decide(controller, ET.tostring(root, encoding="unicode"))


def test_floating_window_covering_target_stops():
    root = ET.fromstring(home())
    root[0].append(node(desc="floatingWindow", bounds="[100,220][800,330]"))
    with pytest.raises(DianpingConditionError, match="浮层"):
        decide(
            DianpingController("广州", "霸王茶姬"),
            ET.tostring(root, encoding="unicode"),
        )


@pytest.mark.parametrize(
    "bad_xml",
    ["<hierarchy>", "<node/>", "<!DOCTYPE x><hierarchy/>", xml(node(text="未知页面"))],
)
def test_unknown_or_malformed_hierarchy_never_guesses(bad_xml: str):
    with pytest.raises(DianpingConditionError):
        decide(DianpingController("广州", "霸王茶姬"), bad_xml)


def test_mismatching_screen_geometry_is_rejected():
    with pytest.raises(DianpingConditionError, match="几何"):
        DianpingController("广州", "霸王茶姬").decide(home(), 1200, 2412)
