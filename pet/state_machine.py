"""状态机：数据驱动（参考 GroupFinity Shimeji 魔理沙 動作.xml）。

设计：
- 静态姿态（stand/sit/relax/lie/think...）与循环移动（walk/run/crawl/hang/climb/carry...）
  统一用序列 = [(图片, tick数, dx, dy)] 描述，数据源 pet/anim_data.py。
- tick(dt) 返回 (QPixmap, dx, dy)；dx/dy 为“按当前朝向折算后”的位移建议。
- 一次性脚本播完自动回到 before_pose（或指定状态）。
"""
import random
from enum import Enum
from typing import List, Optional, Tuple
from PyQt6.QtGui import QPixmap
from .sprite_loader import SpriteLoader
from .config import CONFIG
from . import anim_data

_STATIC_POSES = {
    "stand": "立つ",      # shime1 发呆站立
    "sit": "座る",        # shime11 普通坐
    "relax": "楽に座る",  # shime30 放松坐
    "lie": "寝そべる",    # shime21 卧躺
    "think": "座って見上げる",  # shime26 抬头沉思
    "hang": "天井に掴まる",     # 23/24/25 吊挂
    "push": "IEを投げる",       # shime37 推窗
    "carry_down": "IEを持って落ちる",  # shime47 抱窗下落
    "grip": "つままれる",       # 被提溜（默认档）
}

_MOVE_ACTIONS = {
    "walk": "歩く",
    "run": "走る",
    "crawl": "ずりずり",
    "crawl_ceiling": "天井を伝う",
    "climb": "壁を登る",
    "carry_walk": "IEを持って歩く",
    "look": "座って首が回る",
    "feet": "足をぶらぶらさせる",
    "bounce": "跳ねる",
    "tumble": "転ぶ",
    "stand_look": "座ってマウスを見上げる",
}


class PetState(Enum):
    IDLE = "idle"        # 地面静态待机（pose 区分 stand/sit/relax/lie）
    WALK = "walk"
    RUN = "run"
    CRAWL = "crawl"
    SIT = "sit"
    RELAX = "relax"
    LIE = "lie"
    DANGLE = "dangle"    # 坐着晃脚
    LOOK = "look"        # 转头看鼠标/东张西望（脚本）
    BOUNCE = "bounce"
    TUMBLE = "tumble"
    RIDING = "riding"    # 骑窗口顶/吊窗口下/吊屏顶/爬侧壁（附几何由窗口层管理）
    HANG = "hang"        # 吊挂不动/横移（RIDING 的动画层也可用）
    CLIMB = "climb"
    CARRY = "carry"      # 扛窗走
    DRAGGED = "dragged"
    FALL = "fall"
    LANDING = "landing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class StateMachine:
    def __init__(self, loader: SpriteLoader):
        self.loader = loader
        self.state: PetState = PetState.IDLE
        self.facing_right: bool = False

        # 当前待机姿态名（stand/sit/relax/lie）
        self.idle_pose: str = "stand"
        # 说话/脚本前的姿态，用于结束后恢复
        self._pose_backup: str = "stand"

        # 序列推进
        self._seq: List[Tuple[str, int, int, int]] = []  # (img, ticks, dx, dy)
        self._seq_idx: int = 0
        self._seq_ticks: int = 0
        self._loop: bool = False
        self._script_once: bool = False

        # 拖拽摆动参数
        self.drag_swing_offset: float = 0.0
        self.is_struggling: bool = False
        self.struggle_timer: int = 0

        # 帧动画计时（静态抖动用）
        self.ticks_in_frame: int = 0

    # ---------------- 序列辅助 ----------------
    def _poses(self, key: str) -> List[Tuple[str, int, int, int]]:
        """取 anim_data 动作的普通(无条件)第一组序列 [(img,dur,dx,dy)]"""
        act = anim_data.ACTIONS.get(key)
        if not act:
            return []
        for an in act["anims"]:
            if not an["cond"]:
                return [(p[0], p[1], p[2], p[3]) for p in an["poses"]]
        # 全都有条件就用第一组
        first = act["anims"][0]
        return [(p[0], p[1], p[2], p[3]) for p in first["poses"]]

    def _set_script(self, seq, loop=False):
        self._seq = list(seq)
        self._seq_idx = 0
        self._seq_ticks = 0
        self._loop = loop

    def _static(self, key: str):
        img = anim_data.ACTIONS[key]["anims"][0]["poses"][0][0]
        self._seq = [(img, 1, 0, 0)]
        self._seq_idx = 0
        self._seq_ticks = 0
        self._loop = True  # 静态保持

    def _set_pose(self, pose: str):
        """切到地面静态待机姿态并标记 IDLE。"""
        self.idle_pose = pose
        self.state = PetState.IDLE
        self._static(_STATIC_POSES[pose])

    # ---------------- 对外状态切换 ----------------
    def set_state(self, new_state: PetState, facing_right: bool = None):
        if facing_right is not None:
            self.facing_right = facing_right

        # 高交互状态优先被覆盖时不重复打断（拖拽/骑乘之外一律允许重进）
        if self.state == new_state and new_state not in (PetState.IDLE, PetState.SIT):
            return

        self.state = new_state
        self._loop = False

        if new_state == PetState.IDLE:
            # 保持当前 idle_pose（由调用方选姿或走 director）
            self._static(_STATIC_POSES.get(self.idle_pose, "立つ"))

        elif new_state == PetState.SIT:
            self.idle_pose = "sit"
            self._static("座る")

        elif new_state == PetState.RELAX:
            self.idle_pose = "relax"
            self._static("楽に座る")

        elif new_state == PetState.LIE:
            self.idle_pose = "lie"
            self._static("寝そべる")

        elif new_state == PetState.DANGLE:
            self.idle_pose = "sit"
            self._set_script(self._poses("足をぶらぶらさせる"), loop=True)

        elif new_state == PetState.WALK:
            self._set_script(self._poses("歩く"), loop=True)

        elif new_state == PetState.RUN:
            self._set_script(self._poses("走る"), loop=True)

        elif new_state == PetState.CRAWL:
            self._set_script(self._poses("ずりずり"), loop=True)

        elif new_state == PetState.LOOK:
            self._pose_backup = self.idle_pose
            self._set_script(self._poses("座って首が回る"), loop=True)

        elif new_state == PetState.BOUNCE:
            self._pose_backup = self.idle_pose
            self._set_script(self._poses("跳ねる"), loop=False)

        elif new_state == PetState.TUMBLE:
            self._pose_backup = self.idle_pose
            self._set_script(self._poses("転ぶ"), loop=False)

        elif new_state == PetState.HANG:
            # 吊挂静止（横移由 pet_window 切换 crawl_ceiling 循环）
            self._static("天井に掴まる")

        elif new_state == PetState.CLIMB:
            self._set_script(self._poses("壁を登る"), loop=True)

        elif new_state == PetState.CARRY:
            self._set_script(self._poses("IEを持って歩く"), loop=True)

        elif new_state == PetState.RIDING:
            self.idle_pose = "sit"
            self._static("座る")

        elif new_state == PetState.THINKING:
            self._static("座って見上げる")

        elif new_state == PetState.SPEAKING:
            # 说话小幅动嘴（原 speak 帧组合）
            self._set_script([
                ("shime1.png", 6, 0, 0),
                ("shime26.png", 6, 0, 0),
                ("shime1.png", 6, 0, 0),
                ("shime40.png", 8, 0, 0),
                ("shime41.png", 8, 0, 0),
            ], loop=True)

        elif new_state == PetState.DRAGGED:
            pass  # 拖拽帧在 tick 内特判

        elif new_state == PetState.FALL:
            self._static("落ちる")  # shime53

        elif new_state == PetState.LANDING:
            self._pose_backup = self.idle_pose
            self._set_script(self._poses("跳ねる"), loop=False)

    # ---------------- 一次性动作 ----------------
    def act_once(self, state: PetState):
        """跑一个不循环的肢体脚本（弹跳/翻滚等），结束回 IDLE 姿态。"""
        self.set_state(state)

    def change_hang_motion(self, moving: bool):
        """吊挂时切换：静止(抓稳) ↔ 横移(天井を伝う)。"""
        if self.state not in (PetState.HANG, PetState.RIDING):
            return
        if moving:
            self._set_script(self._poses("crawl_ceiling"), loop=True)
        else:
            self._static("天井に掴まる")

    def climb_pose(self):
        self._set_script(self._poses("壁を登る"), loop=True)

    # ---------------- 每帧推进 ----------------
    def update_drag_velocity(self, dx: float):
        self.drag_swing_offset = dx
        self.struggle_timer += 1
        if self.struggle_timer > 30:
            self.is_struggling = (self.struggle_timer % 12 < 6)

    def tick(self, dt_sec: float) -> Tuple[QPixmap, int, int]:
        """返回 (当前帧, dx, dy)。dx/dy 已按朝向折算。"""
        dx = dy = 0

        # --- 拖拽特判 ---
        if self.state == PetState.DRAGGED:
            if self.is_struggling:
                img_name = "shime5.png" if (self.ticks_in_frame % 8 < 4) else "shime6.png"
            elif self.drag_swing_offset < -8:
                img_name = "shime9.png"
            elif self.drag_swing_offset > 8:
                img_name = "shime8.png"
            elif abs(self.drag_swing_offset) > 2:
                img_name = "shime7.png"
            else:
                img_name = "shime48.png"
            self.ticks_in_frame += 1
            return self.loader.get_frame(img_name, self.facing_right), 0, 0

        # --- 下落/静态直接返回 ---
        if self.state == PetState.FALL:
            return self.loader.get_frame("shime53.png", self.facing_right), 0, 0

        # --- 序列推进 ---
        if not self._seq:
            return self.loader.get_frame("shime1.png", self.facing_right), 0, 0

        img, max_ticks, step_x, step_y = self._seq[self._seq_idx]
        self._seq_ticks += 1

        if self._seq_ticks >= max_ticks:
            self._seq_ticks = 0
            self._seq_idx += 1
            if self._seq_idx >= len(self._seq):
                if self._loop:
                    self._seq_idx = 0
                else:
                    # 一次性脚本结束：_on_script_done 会换回静态姿态序列
                    self._seq_idx = 0
                    self._on_script_done()
            # 重新读（可能已被 done 回调替换序列）
            self._seq_idx = min(self._seq_idx, len(self._seq) - 1)

        img, max_ticks, step_x, step_y = self._seq[self._seq_idx]
        if self.state in (PetState.WALK, PetState.RUN, PetState.CRAWL, PetState.HANG, PetState.CARRY):
            # 水平移动按朝向取绝对值方向
            dx = abs(step_x) if self.facing_right else -abs(step_x)
            dy = 0
        elif self.state == PetState.CLIMB:
            dx = 0
            dy = -abs(step_y) if step_y != 0 else 0  # 向上爬；向下由调用方翻负号
        else:
            dx = 0
            dy = 0

        return self.loader.get_frame(img, self.facing_right), dx, dy

    def _on_script_done(self):
        """一次性脚本结束回调：回地面姿态或特定状态。"""
        if self.state == PetState.BOUNCE:
            self._restore_idle_pose()
        elif self.state == PetState.TUMBLE:
            self._restore_idle_pose()
        elif self.state == PetState.LANDING:
            self._restore_idle_pose()
        elif self.state == PetState.LOOK:
            pass  # LOOK 常循环由调用方手动退出

    def _restore_idle_pose(self):
        self.state = PetState.IDLE
        self._static(_STATIC_POSES.get(self.idle_pose, "立つ"))

    def play_raw_cycles(self, action_name: str, cycles: int = 1):
        """播放某个动作序列 N 遍后恢复地面站姿（用于演出，如 Master Spark 42-46）。"""
        poses = self._poses(action_name)
        if not poses:
            return False
        seq = list(poses) * max(1, cycles)
        self._pose_backup = self.idle_pose
        self.set_idle_pose("stand")
        self.state = PetState.BOUNCE  # 复用“播完自动恢复”路径
        self._set_script(seq, loop=False)
        return True

    def pose_blip(self, img: str, duration_ticks: int = 12, back_pose: str = "stand"):
        """短暂定格某一帧（如 47 抱持收尾），结束后恢复指定地面姿态。"""
        self.idle_pose = back_pose
        self.state = PetState.BOUNCE  # 复用自动恢复
        self._set_script([(img, max(1, duration_ticks), 0, 0)], loop=False)

    def set_carry_fast(self, fast: bool = True):
        """搬运切换速度档：true 用「扛着跑」帧，false 用「扛着走」帧。"""
        if self.state != PetState.CARRY:
            return
        name = "IEを持って走る" if fast else "IEを持って歩く"
        self._set_script(self._poses(name), loop=True)

    def stop_script_to_idle(self):
        self._restore_idle_pose()

    def stop_motion(self):
        """停止移动态（walk/run/crawl/hang 横移/dangle），回到当前地面姿态。"""
        self.state = PetState.IDLE
        self._static(_STATIC_POSES.get(self.idle_pose, "立つ"))

    def face_toward(self, x_world: int):
        """按世界坐标切换朝向（左=false 右=true）。"""
        self.facing_right = x_world >= 0

    def set_idle_pose(self, pose: str):
        self.idle_pose = pose
        self.state = PetState.IDLE
        self._static(_STATIC_POSES.get(pose, "立つ"))
