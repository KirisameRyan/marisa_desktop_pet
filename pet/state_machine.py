import random
from enum import Enum
from typing import List, Tuple
from PyQt6.QtGui import QPixmap
from .sprite_loader import SpriteLoader
from .config import CONFIG

class PetState(Enum):
    IDLE = "idle"             # 站立待机
    SIT = "sit"               # 坐下小憩
    WALK = "walk"             # 地面漫步
    DRAGGED = "dragged"       # 被鼠标提溜起/拖拽
    FALL = "fall"             # 悬空下落
    LANDING = "landing"       # 触地缓冲
    THINKING = "thinking"     # 思考中 (AI处理/分析)
    SPEAKING = "speaking"     # 说话中 (配合气泡与TTS)

class StateMachine:
    def __init__(self, loader: SpriteLoader):
        self.loader = loader
        self.state: PetState = PetState.IDLE
        self.facing_right: bool = False  # False: 左, True: 右
        
        # 帧动画计时与索引
        self.frame_index: int = 0
        self.ticks_in_frame: int = 0
        self.state_elapsed_sec: float = 0.0
        self.current_state_duration: float = 4.0
        
        # 拖拽摆动参数
        self.drag_swing_offset: float = 0.0
        self.is_struggling: bool = False
        self.struggle_timer: int = 0

        # 定义各状态帧序列: (图片名, 持续tick数)
        self.ANIMATIONS = {
            # 站立待机：轻微眨眼/呼吸节奏
            "stand": [
                ("shime1.png", 50),
                ("shime1.png", 50),
            ],
            # 坐下晃脚
            "sit_chill": [
                ("shime11.png", 40),
                ("shime31.png", 12),
                ("shime32.png", 12),
                ("shime31.png", 12),
                ("shime33.png", 12),
                ("shime11.png", 30),
            ],
            # 站立左右张望
            "look_around": [
                ("shime1.png", 20),
                ("shime26.png", 18),
                ("shime27.png", 18),
                ("shime28.png", 18),
                ("shime29.png", 18),
                ("shime1.png", 20),
            ],
            # 走动
            "walk": [
                ("shime1.png", 6),
                ("shime2.png", 6),
                ("shime1.png", 6),
                ("shime3.png", 6),
            ],
            # 说话中微动
            "speak": [
                ("shime1.png", 6),
                ("shime26.png", 6),
                ("shime1.png", 6),
                ("shime40.png", 8),
                ("shime41.png", 8),
            ],
            # 思考中 (抬头沉思)
            "think": [
                ("shime26.png", 15),
                ("shime27.png", 15),
                ("shime28.png", 15),
                ("shime27.png", 15),
            ],
            # 触地缓冲
            "landing": [
                ("shime18.png", 4),
                ("shime19.png", 4),
                ("shime49.png", 4),
            ]
        }
        self.sub_anim_name: str = "stand"

    def set_state(self, new_state: PetState, facing_right: bool = None):
        if facing_right is not None:
            self.facing_right = facing_right
        if self.state == new_state and new_state not in (PetState.IDLE, PetState.SIT):
            return
        
        self.state = new_state
        self.frame_index = 0
        self.ticks_in_frame = 0
        self.state_elapsed_sec = 0.0

        if new_state == PetState.IDLE:
            # 待机时随机挑选细分子动画
            dice = random.random()
            if dice < 0.5:
                self.sub_anim_name = "stand"
            elif dice < 0.8:
                self.sub_anim_name = "look_around"
            else:
                self.sub_anim_name = "sit_chill"
            self.current_state_duration = random.uniform(CONFIG.idle_min_duration, CONFIG.idle_max_duration)

        elif new_state == PetState.SIT:
            self.sub_anim_name = "sit_chill"
            self.current_state_duration = 10.0

        elif new_state == PetState.WALK:
            self.sub_anim_name = "walk"
            self.current_state_duration = random.uniform(CONFIG.walk_min_duration, CONFIG.walk_max_duration)
            # 随机决定走动朝向
            if facing_right is None:
                self.facing_right = random.choice([True, False])

        elif new_state == PetState.SPEAKING:
            self.sub_anim_name = "speak"

        elif new_state == PetState.THINKING:
            self.sub_anim_name = "think"

        elif new_state == PetState.LANDING:
            self.sub_anim_name = "landing"

    def update_drag_velocity(self, dx: float):
        """鼠标拖拽移动时根据横向速度调整摆动帧"""
        self.drag_swing_offset = dx
        self.struggle_timer += 1
        # 长时间拖拽偶发挣扎乱蹬腿
        if self.struggle_timer > 30:
            self.is_struggling = (self.struggle_timer % 12 < 6)

    def tick(self, dt_sec: float) -> Tuple[QPixmap, int]:
        """每帧时钟推进，返回当前帧 QPixmap 和水平位移建议 (dx)"""
        self.state_elapsed_sec += dt_sec
        self.ticks_in_frame += 1
        dx = 0

        # 1. 拖拽状态特殊处理
        if self.state == PetState.DRAGGED:
            if self.is_struggling:
                img_name = "shime5.png" if (self.ticks_in_frame % 8 < 4) else "shime6.png"
            elif self.drag_swing_offset < -8:
                # 快速向左拖 -> 往右甩
                img_name = "shime9.png"
            elif self.drag_swing_offset > 8:
                # 快速向右拖 -> 往左甩
                img_name = "shime8.png"
            elif abs(self.drag_swing_offset) > 2:
                img_name = "shime7.png"
            else:
                img_name = "shime48.png"
            return self.loader.get_frame(img_name, self.facing_right), 0

        # 2. 下落状态
        if self.state == PetState.FALL:
            return self.loader.get_frame("shime53.png", self.facing_right), 0

        # 3. 触地着陆缓冲
        if self.state == PetState.LANDING:
            anim = self.ANIMATIONS["landing"]
            curr_img, max_ticks = anim[self.frame_index]
            if self.ticks_in_frame >= max_ticks:
                self.ticks_in_frame = 0
                self.frame_index += 1
                if self.frame_index >= len(anim):
                    self.set_state(PetState.IDLE)
                    return self.loader.get_frame("shime1.png", self.facing_right), 0
                curr_img, _ = anim[self.frame_index]
            return self.loader.get_frame(curr_img, self.facing_right), 0

        # 4. 行走状态位移
        if self.state == PetState.WALK:
            dx = CONFIG.walk_speed if self.facing_right else -CONFIG.walk_speed
            anim = self.ANIMATIONS["walk"]
            curr_img, max_ticks = anim[self.frame_index]
            if self.ticks_in_frame >= max_ticks:
                self.ticks_in_frame = 0
                self.frame_index = (self.frame_index + 1) % len(anim)
            
            # 时间到切换回待机
            if self.state_elapsed_sec >= self.current_state_duration:
                self.set_state(PetState.IDLE)
            return self.loader.get_frame(curr_img, self.facing_right), dx

        # 5. 待机/思考/说话
        anim_name = self.sub_anim_name
        anim = self.ANIMATIONS.get(anim_name, self.ANIMATIONS["stand"])
        curr_img, max_ticks = anim[self.frame_index]
        if self.ticks_in_frame >= max_ticks:
            self.ticks_in_frame = 0
            self.frame_index = (self.frame_index + 1) % len(anim)

        # 自主行为切换 (Idle -> Walk / Idle -> Look)
        if self.state == PetState.IDLE and self.state_elapsed_sec >= self.current_state_duration:
            # 60% 几率去散步，40% 几率换个待机姿势
            if random.random() < 0.6:
                self.set_state(PetState.WALK)
            else:
                self.set_state(PetState.IDLE)

        return self.loader.get_frame(curr_img, self.facing_right), 0
