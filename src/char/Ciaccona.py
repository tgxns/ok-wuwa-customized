import time
import cv2
import numpy as np
from ok import color_range_to_bound
from src.char.BaseChar import BaseChar, Priority


class Ciaccona(BaseChar):
    """Ciaccona 角色战斗逻辑。

    该角色逻辑的核心点：
    - 会根据队友阵容选择不同“属性/流派”分支（attribute）。
    - 使用更复杂的回路（forte）判定：优先用鼠标重击回路识别，否则对 UI 片段做频域特征判断。
    - 在特定大招持续期间会降低切换优先级，避免被自动切人打断关键站场窗口。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 入场动作的冻结/硬直估计，用于控制入场后补刀/普攻的时机
        self.intro_motion_freeze_duration = 0.73
        # 0: 未决定；1: 默认（风 dot）；2: 光 dot（Phoebe/Zani）；3: 风 dot（Cartethyia 协同）
        self.attribute = 0
        # 标记当前是否处于本角色的大招（共鸣解放）有效动作窗口内
        self.in_liberation = False
        # 在 attribute=3 分支需要判断队友状态（Cartethyia），这里保存引用
        self.cartethyia = None
        # 协奏满触发的延奏/退场时间点，用于判断“处于延奏窗口”
        self.outrotime = -1

    def skip_combat_check(self):
        # 刚释放大招后的短窗口内跳过战斗检测，避免动画/镜头切换导致误判
        return self.time_elapsed_accounting_for_freeze(self.last_liberation) < 2

    def reset_state(self):
        super().reset_state()
        self.attribute = 0
        self.cartethyia = None

    def do_perform(self):
        """上场后的主要输出流程。"""
        self.in_liberation = False
        # wait：某些动作后需要小停顿再开大，保证按键/动画衔接稳定
        wait = False
        # jump：需要时通过跳跃进入空中动作窗口（某些回路/动作需要空中触发）
        jump = True
        if self.attribute == 0:
            self.decide_teammate()
        if self.has_intro:
            # 有入场技时先补一段普攻；如果不处于“需要快速切人”状态，再额外多打一段
            self.continues_normal_attack(0.8)
            if not self.need_fast_perform():
                self.continues_normal_attack(0.7)
        if self.current_echo() < 0.22:
            # 声骸可瞬发时尝试直接点一次（time_out=0 表示不做等待循环）
            self.click_echo(time_out=0)
        if not self.has_intro and not self.need_fast_perform() and not self.is_mouse_forte_full():
            # 非入场且不需要抢切时，尝试跳+点以触发/加速回路积累，并等待落地
            self.click_jump_with_click(0.4)
            self.task.wait_until(lambda: not self.flying(), post_action=self.click_with_interval, time_out=1.2)
            self.continues_normal_attack(0.2)
        if self.click_resonance()[0]:
            # 成功释放共鸣技能后，不再额外起跳；并设置 wait，为后续开大留出稳定窗口
            jump = False
            wait = True
        if self.judge_forte() >= 3:
            # 回路达到阈值：尽量进入空中后执行“重击回路”，用于释放关键动作
            if jump:
                start = time.time()
                while not self.flying():
                    self.task.jump(after_sleep=0.01)
                    if time.time() - start > 0.3:
                        break
                    self.task.next_frame()
            self.heavy_click_forte(check_fun=self.is_mouse_forte_full)
            wait = True
        if self.liberation_available():
            if wait:
                self.sleep(0.4)
            if self.click_liberation():
                self.in_liberation = True
                if self.attribute == 2:
                    # 光 dot 分支：开大后短时间持续按 A（通常用于触发特定动作/位移/取消后摇）
                    self.continues_click_a(0.6)
        if not self.in_liberation and self.current_echo() > 0.25:
            # 不在大招动作窗口时，若声骸可用则释放
            self.click_echo()
        self.switch_next_char()

    def do_get_switch_priority(self, current_char: BaseChar, has_intro=False, target_low_con=False):
        """切换到该角色的优先级计算。

        关键：在部分分支的大招持续窗口内，直接返回最低优先级，避免被切上/切下打断。
        """
        if self.attribute == 2 and self.in_liberation and self.time_elapsed_accounting_for_freeze(
                self.last_liberation) < 20:
            return Priority.MIN
        if self.attribute == 3:
            self.logger.debug(f'ciaccona cond: {self.cartethyia.is_cartethyia}')
        if self.attribute == 3 and self.in_liberation and (
                self.time_elapsed_accounting_for_freeze(self.last_liberation) < 8 or not self.cartethyia.is_cartethyia):
            return Priority.MIN
        return super().do_get_switch_priority(current_char, has_intro)

    def click_jump_with_click(self, delay=0.1):
        """在一个短时间窗口内交替执行“跳跃/点击”，用于触发空中状态与补帧点击。"""
        start = time.time()
        click = 1
        while True:
            if time.time() - start > delay:
                break
            if click == 0:
                self.task.jump(after_sleep=0.01)
            else:
                self.click()
            click = 1 - click
            self.check_combat()
            self.task.next_frame()

    def continues_click_a(self, duration=0.6):
        """持续发送 A 键一段时间。"""
        start = time.time()
        while time.time() - start < duration:
            self.task.send_key(key='a')

    def judge_forte(self):
        """判断回路格数/强度。

        优先使用更准确的“鼠标回路”识别；否则对回路 UI 做频域特征判断。
        """
        if self.is_mouse_forte_full():
            return 3
        box = self.task.box_of_screen_scaled(3840, 2160, 1612, 1987, 2188, 2008, name='ciaccona_forte', hcenter=True)
        forte = self.calculate_forte_num(ciaccona_forte_color, box, 3, 12, 14, 100)
        return forte

    def decide_teammate(self):
        """根据队友阵容决定 attribute 分支，并记录需要的队友引用。"""
        from src.char.Phoebe import Phoebe
        from src.char.Zani import Zani
        from src.char.Cartethyia import Cartethyia
        for i, char in enumerate(self.task.chars):
            self.logger.debug(f'ciaccona teammate char: {char.char_name}')
            if isinstance(char, (Cartethyia)):
                self.logger.debug('ciaccona set attribute: wind dot')
                self.cartethyia = char
                self.attribute = 3
                return
            if isinstance(char, (Phoebe, Zani)):
                self.logger.debug('ciaccona set attribute: light dot')
                self.attribute = 2
                return
        self.logger.debug('ciaccona set attribute: wind dot')
        self.attribute = 1
        return

    def judge_frequncy_and_amplitude(self, gray, min_freq, max_freq, min_amp):
        """对二值图像列轮廓做频域特征判断。

        gray 预期是仅包含 0/255 的二值图像切片。通过 FFT 找到主频与幅值，用于
        区分不同“格数/条纹密度”的回路填充状态（比简单像素占比更稳）。
        """
        height, width = gray.shape[:]
        if height == 0 or width < 64 or not np.array_equal(np.unique(gray), [0, 255]):
            return 0
        profile = np.sum(gray == 255, axis=0).astype(np.float32)
        profile -= np.mean(profile)
        n = np.abs(np.fft.fft(profile))
        amplitude = 0
        frequncy = 0
        i = 1
        while i < width:
            if n[i] > amplitude:
                amplitude = n[i]
                frequncy = i
            i += 1
        self.logger.debug(f'forte with freq {frequncy} & amp {amplitude}')
        return (min_freq <= frequncy <= max_freq) or amplitude >= min_amp

    def calculate_forte_num(self, forte_color, box, num=1, min_freq=39, max_freq=41, min_amp=50):
        """估算回路格数。

        将目标 UI 区域按 num 等分，从右往左逐段检测；找到第一个满足频域特征的段，
        视为当前回路格数（forte）。
        """
        cropped = box.crop_frame(self.task.frame)
        lower_bound, upper_bound = color_range_to_bound(forte_color)
        image = cv2.inRange(cropped, lower_bound, upper_bound)

        height, width = image.shape
        step = int(width / num)

        forte = num
        left = step * (forte - 1)
        while forte > 0:
            gray = image[:, left:left + step]
            score = self.judge_frequncy_and_amplitude(gray, min_freq, max_freq, min_amp)
            if score:
                break
            left -= step
            forte -= 1
        self.logger.info(f'Frequncy analysis with forte {forte}')
        return forte

    def switch_next_char(self, *args):
        # 协奏满则记录退场时间，用于后续判断“延奏窗口”
        if self.is_con_full():
            self.outrotime = time.time()
        return super().switch_next_char(*args)

    def in_outro(self):
        """是否仍在延奏/退场后的窗口期内。"""
        return self.time_elapsed_accounting_for_freeze(self.outrotime) < 30


ciaccona_forte_color = {
    'r': (70, 100),  # Red range
    'g': (240, 255),  # Green range
    'b': (180, 210)  # Blue range
}
