# -*- coding: utf-8 -*-
"""
模板图采集工具（油学通签到版）
运行后按提示操作：把鼠标移到目标左上角按 F8、右下角按 F8，自动裁剪保存模板图（全程不抢微信焦点，搜索下拉栏不会消失）。
换到新机器（或屏幕分辨率/缩放与采集时不同）后，必须重新采集 5 张油学通专属模板（搜索图标 + 日常管理 + 签到消息 + 每日签到 + 进入按钮），
否则脚本找不到小程序图标与各级签到入口，会直接失败。
另有 2 张微信通用模板（进入微信确认键 / 顶部搜索框），若你的微信界面没变化可按 F9 跳过。
"""
import os
import sys
import time
import ctypes
import pyautogui
from pathlib import Path

# 高分屏/缩放适配：让本进程按物理像素工作，否则系统缩放 125%/150% 时，
# 鼠标坐标与截图像素会整体错位，框选出来的模板是歪的、后面匹配永远对不上。
def _enable_dpi_awareness():
    cands = [
        lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
        lambda: ctypes.windll.user32.SetProcessDPIAware(),
    ]
    for fn in cands:
        try:
            fn(); return
        except Exception:
            continue
_enable_dpi_awareness()

SCRIPT_DIR = Path(__file__).parent.resolve()
TEMPLATE_DIR = SCRIPT_DIR / "templates"
TEMPLATE_DIR.mkdir(exist_ok=True)

# 需要采集的模板（按签到执行顺序排列）
#   - 前两项（00 进入微信 / 07 搜索框）是微信通用界面：若你的微信和内置模板一致可按 F9 跳过；
#   - 后面 5 张油学通专属，必须采集。
TEMPLATES = [
    {
        "file": "00_enter_wechat.png",
        "name": "【可跳过】微信“进入微信”确认按钮",
        "desc": "微信有时会弹“进入微信”的确认页，框住那个确认按钮；你登录时若没这个页就按 F9 跳过",
        "pre_action": "先退出微信再打开，停在“进入微信”确认页（没有该页就按 F9 跳过）",
    },
    {
        "file": "07_search_box.png",
        "name": "【可跳过】微信顶部搜索框",
        "desc": "微信主界面顶部的搜索框（含“搜索”占位文字的区域）；若你微信界面和内置模板一致可按 F9 跳过",
        "pre_action": "切到微信主界面，保持顶部搜索框可见",
    },
    {
        "file": "02_miniprogram_icon.png",
        "name": "【必采】油学通小程序图标（搜索结果下拉栏）",
        "desc": "微信搜“油学通”后、下拉结果面板里冒出的那个油学通小图标（还没进小程序的），只框图标本身，可连同右下角“小程序”角标一起框",
        "pre_action": "在微信顶部搜索框输入“油学通”（千万别按回车、别点别处），让搜索结果下拉面板一直显示在屏幕上",
    },
    {
        "file": "21_daily_manage.png",
        "name": "【必采】工作台里的“日常管理”入口",
        "desc": "油学通工作台首页里的“日常管理”图标/入口，把图标+“日常管理”文字一起框住",
        "pre_action": "打开油学通小程序，停在【工作台】首页，确保“日常管理”可见（在下方就先滚到能看到它）",
    },
    {
        "file": "22_signin_msg.png",
        "name": "【必采】“日常管理”里的“签到消息”入口",
        "desc": "点进“日常管理”后看到“签到消息”那一项，把图标+“签到消息”文字一起框住",
        "pre_action": "点进“日常管理”，停在能看到“签到消息”的页面",
    },
    {
        "file": "23_daily_signin.png",
        "name": "【必采】“签到消息”里的“每日签到”入口",
        "desc": "点进“签到消息”后看到“每日签到”那一项，把图标+“每日签到”文字一起框住",
        "pre_action": "点进“签到消息”，停在能看到“每日签到”的页面",
    },
    {
        "file": "24_signin_enter.png",
        "name": "【必采】点“每日签到”后、还要再点一下才能进入签到页的那个入口/按钮",
        "desc": "点进“每日签到”后会出现一个还需要再点一下才能进入签到详情页（签到/请假）的东西，把要点的那个整体框住",
        "pre_action": "点进“每日签到”，停在那个“再点一下才进入”的页面，框住要点的按钮/入口",
    },
]

VK_F8 = 0x77
VK_F9 = 0x78

def _key_down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

def wait_any_key(vks, labels):
    """等待 vks 中任一键完成一次“按下+抬起”，返回该键虚拟键码。用全局键、不抢窗口焦点。"""
    time.sleep(0.05)
    for vk in vks:
        while _key_down(vk):
            time.sleep(0.02)
    while True:
        for vk in vks:
            if _key_down(vk):
                while _key_down(vk):
                    time.sleep(0.02)
                return vk
        time.sleep(0.03)

def capture_template(tpl_info):
    """采集单个模板：F8 记左上/右下，F9 跳过，全程不抢微信焦点"""
    print("\n" + "=" * 60)
    print(f"采集: {tpl_info['name']}")
    print(f"说明: {tpl_info['desc']}")
    print("=" * 60)
    print(f"\n准备: {tpl_info['pre_action']}")
    print("\n操作（全程用键盘，不要点别处，微信下拉栏就不会消失）：")
    print("  1. 鼠标移到目标【左上角】 -> 按 F8")
    print("  2. 鼠标移到目标【右下角】 -> 再按 F8")
    print("  想跳过这一张 -> 按 F9")
    while True:
        k = wait_any_key([VK_F8, VK_F9], ["F8", "F9"])
        if k == VK_F9:
            print("    已跳过本项")
            return False
        x1, y1 = pyautogui.position()
        print(f"    左上角已记录 ({x1},{y1})；把鼠标移到右下角后再按 F8")
        k2 = wait_any_key([VK_F8, VK_F9], ["F8", "F9"])
        if k2 == VK_F9:
            print("    已取消本张，重来")
            continue
        x2, y2 = pyautogui.position()

        left = min(x1, x2); right = max(x1, x2)
        top = min(y1, y2); bottom = max(y1, y2)

        if right - left < 20 or bottom - top < 20:
            print(f"    警告：框的区域只有 {right-left}x{bottom-top} 太小，容易误判。")
            print("    （按 F8 重新框本张，按 F9 跳过）")
            k3 = wait_any_key([VK_F8, VK_F9], ["F8", "F9"])
            if k3 == VK_F9:
                return False
            continue

        screenshot = pyautogui.screenshot()
        template = screenshot.crop((left, top, right, bottom))
        save_path = TEMPLATE_DIR / tpl_info["file"]
        template.save(str(save_path))
        print(f"\n    模板已保存: {save_path}")
        print(f"    尺寸: {template.size}")
        return True

def main():
    print("=" * 60)
    print("油学通签到脚本 - 模板图采集工具")
    print("=" * 60)
    print(f"\n模板保存目录: {TEMPLATE_DIR}")
    print(f"\n共 {len(TEMPLATES)} 项模板。带【必采】的 5 张必须采集（顺序就是签到点击顺序）；带【可跳过】的 2 张微信通用，界面没变可按 F9 跳过：")
    for i, t in enumerate(TEMPLATES, 1):
        print(f"  {i}. {t['name']}  ->  {t['file']}")

    print("\n注意事项:")
    print("  - 采集过程中不要移动微信窗口、不要点别处")
    print("  - 尽量只框选按钮/图标本身，不要包含太多背景")
    print("  - 确认位置用 F8，跳过某张用 F9（不再用回车，避免把微信下拉栏收掉）")
    print("  - 采错了重新运行本工具覆盖即可")
    print("\n准备好后按 F8 开始采集...")
    wait_any_key([VK_F8], ["F8"])

    success_count = 0
    for tpl in TEMPLATES:
        try:
            if capture_template(tpl):
                success_count += 1
        except KeyboardInterrupt:
            print("\n用户中断")
            break
        except Exception as e:
            print(f"采集失败: {e}")

    print("\n" + "=" * 60)
    print(f"采集完成: 成功 {success_count}/{len(TEMPLATES)}")
    print("5 张【必采】都采完、且框得尽量只含图标/文字后，即可运行 2_手动测试签到(管理员).bat 测试。")
    print("采错了没关系，重跑本工具覆盖对应模板即可。")
    print("=" * 60)
    input("\n按回车键退出...")

if __name__ == "__main__":
    main()