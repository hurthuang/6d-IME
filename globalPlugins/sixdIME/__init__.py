# -*- coding: UTF-8 -*-
# 6d-IME — 六點點字輸入 NVDA globalPlugin  v10l
# 完全重寫，以 v6b 為基礎，整合所有修正
#
# 架構說明：
#   注音模式：Phn.tbl 查表 → SendInput 送大千鍵
#   標點模式：buf空時累積格序列，空白鍵確認送出（雙格序列按第二格立即送）
#   comp8/UEB-G2：brailleInput.handler.input(dots) 直接呼叫
#
# 標點設計（完全採用 BrlIMEHelper bopomofo.json 的格序列）：
#   雙格→立即送出：：「」『』《》—
#   單格+空白：，、。；（）！？【】〈〉…
#
# 標點衝突解法：
#   標點路徑只在「_brl_buf 空白」時啟動
#   若格序列中途出現注音格（有 Phn.tbl 命中）→ 轉注音邏輯
#   調號格遇到時：先清標點序列，再處理調號

from __future__ import unicode_literals

import ctypes
import ctypes.wintypes
import os
import winsound

import brailleInput
import globalPluginHandler
import gui
import louis
import queueHandler
import speech
import ui
import winInputHook
import wx

from brailleTables import getTable as getBRLtable
from logHandler import log


# ═══════════════════════════════════════════════════════════
# 模式常數
# ═══════════════════════════════════════════════════════════
MODE_BOPOMOFO   = 0
MODE_BRL_UNICODE= 1   # 直接輸出 Unicode 點字字元 ⠀-⠿
MODE_COMP8      = 2
MODE_UEB_G1     = 3
MODE_UEB_G2     = 4

_MODE_NAMES = {
    None:             '一般輸入法',
    MODE_BOPOMOFO:    '注音點字',
    MODE_BRL_UNICODE: '黑點點字',
    MODE_COMP8:       '電腦點字',
    MODE_UEB_G1:      'UEB一級',
    MODE_UEB_G2:      'UEB二級',
}
_MODE_TABLES = {
    MODE_COMP8:  'en-us-comp8-ext.utb',
    MODE_UEB_G1: 'en-ueb-g1.ctb',
    MODE_UEB_G2: 'en-ueb-g2.ctb',
}
# toggle 循環的完整順序（含 None = 一般輸入法，放最前面）
_ALL_MODES_WITH_OFF = [None, MODE_BOPOMOFO, MODE_BRL_UNICODE, MODE_COMP8, MODE_UEB_G1, MODE_UEB_G2]
# 需要 IME 切換和 table 切換的模式
_ENG_MODES = frozenset({MODE_COMP8, MODE_UEB_G1, MODE_UEB_G2})


# ═══════════════════════════════════════════════════════════
# NABCC 對照表（64 格，6-bit）
# ═══════════════════════════════════════════════════════════
_NABCC = list(" a1b'k2l@cif/msp\"e3h9o6r\\djg>ntq,*5<-u8v.%[$+x!&;:4|0z7(_?w]#y)=")


# ═══════════════════════════════════════════════════════════
# 注音調號（五單鍵）
# ═══════════════════════════════════════════════════════════
_TONE_DIRECT = {
    4:  ' ',   # s → 第1聲
    2:  '6',   # d → ˊ 第2聲
    8:  '3',   # j → ˇ 第3聲
    16: '4',   # k → ˋ 第4聲
}
_BITS1_TONE = '7'   # f(bits=1) + buf有內容 → 輕聲


# ═══════════════════════════════════════════════════════════
# 標點序列表（完全採用 BrlIMEHelper 格序列）
#
# 規則：
#   雙格序列 → 輸入第二格後立即送出（不需空白）
#   單格序列 → 格 + 空白鍵 → 送出
#   有衝突的格（如 21=ㄅ前綴）由 _try_punct_or_bopo 決定走哪條路
#
# bits 對應：
#   ⠆ = 點2+3 = bits=6     ⠠ = 點6   = bits=32
#   ⠤ = 點3+6 = bits=36    ⠰ = 點5+6 = bits=48
#   ⠒ = 點2+5 = bits=18    ⠹ = 點1+4+5+6 = bits=57
#   ⠇ = 點1+2+3 = bits=7   ⠕ = 點1+3+5 = bits=21
#   ⠪ = 點2+4+6 = bits=42  ⠽ = 點1+3+4+5+6 = bits=61
#   ⠣ = 點1+2+6 = bits=35  ⠜ = 點3+4+5 = bits=28
#   ⠯ = 點1+2+3+4+6 = bits=47
#   ⠝ = 點1+3+4+5 = bits=29 ← 也是？
# ═══════════════════════════════════════════════════════════
_PUNCT_SEQ = {
    # 單格 + 空白 → 送標點
    (6,):   '，',   # ⠆⠀
    (32,):  '、',   # ⠠⠀
    (36,):  '。',   # ⠤⠀  （也是 」 第一格：⠤⠆）
    (48,):  '；',   # ⠰⠀  （也是 「 第一格：⠰⠤）
    (7,):   '！',   # ⠇⠀
    (57,):  '？',   # ⠹⠀
    (21,):  '）',   # ⠕⠀
    (42,):  '（',   # ⠪⠀
    (61,):  '】',   # ⠽⠀
    (47,):  '【',   # ⠯⠀
    (35,):  '〈',   # ⠣⠀
    (28,):  '〉',   # ⠜⠀
    # 雙格 → 立即送出（輸入第二格後馬上輸出）
    (18, 18):   '：',   # ⠒⠒
    (48, 36):   '「',   # ⠰⠤
    (36,  6):   '」',   # ⠤⠆
    (38, 38):   '『',   # ⠦⠦  點2+3+6 × 2
    (52, 52):   '』',   # ⠴⠴  點3+5+6 × 2
    (35, 35):   '《',   # ⠣⠣
    (28, 28):   '》',   # ⠜⠜
    (16,  2):   '—',   # ⠐⠂  點5 + 點2
}

# 前綴集合（加速判斷）
_PUNCT_PREFIXES = set()
for _k in _PUNCT_SEQ:
    for _i in range(1, len(_k) + 1):
        _PUNCT_PREFIXES.add(_k[:_i])

# 立即送出規則：雙格（或更多格）序列輸入最後一格後立即送出，不等空白
# 單格序列：需等空白鍵確認
# 實作：直接用 len(key) >= 2 判斷，無需預計算
_PUNCT_IMMEDIATE: set = frozenset(k for k in _PUNCT_SEQ if len(k) >= 2)


# ═══════════════════════════════════════════════════════════
# 六點鍵 VK → bitmask
# ═══════════════════════════════════════════════════════════
_BRL_KEY_VK = {
    0x46: 1 << 0,   # f = 點1
    0x44: 1 << 1,   # d = 點2
    0x53: 1 << 2,   # s = 點3
    0x4A: 1 << 3,   # j = 點4
    0x4B: 1 << 4,   # k = 點5
    0x4C: 1 << 5,   # l = 點6
}
VK_SPACE = 0x20


# ═══════════════════════════════════════════════════════════
# 大千鍵位（VK, scan）
# ═══════════════════════════════════════════════════════════
_DAQIAN_KEY = {
    '1':(0x31,0x02), '2':(0x32,0x03), '3':(0x33,0x04),
    '4':(0x34,0x05), '5':(0x35,0x06), '6':(0x36,0x07),
    '7':(0x37,0x08), '8':(0x38,0x09), '9':(0x39,0x0A),
    '0':(0x30,0x0B),
    'q':(0x51,0x10), 'w':(0x57,0x11), 'e':(0x45,0x12),
    'r':(0x52,0x13), 't':(0x54,0x14), 'y':(0x59,0x15),
    'u':(0x55,0x16), 'i':(0x49,0x17), 'o':(0x4F,0x18),
    'p':(0x50,0x19),
    'a':(0x41,0x1E), 's':(0x53,0x1F), 'd':(0x44,0x20),
    'f':(0x46,0x21), 'g':(0x47,0x22), 'h':(0x48,0x23),
    'j':(0x4A,0x24), 'k':(0x4B,0x25), 'l':(0x4C,0x26),
    'z':(0x5A,0x2C), 'x':(0x58,0x2D), 'c':(0x43,0x2E),
    'v':(0x56,0x2F), 'b':(0x42,0x30), 'n':(0x4E,0x31),
    'm':(0x4D,0x32),
    '-':(0xBD,0x0C), '=':(0xBB,0x0D),
    '[':(0xDB,0x1A), ']':(0xDD,0x1B),
    ';':(0xBA,0x27), "'":(0xDE,0x28),
    ',':(0xBC,0x33), '.':(0xBE,0x34), '/':(0xBF,0x35),
    '`':(0xC0,0x29), '\\':(0xDC,0x2B),
    ' ':(0x20,0x39),
}


# ═══════════════════════════════════════════════════════════
# SendInput（繞過 NVDA inputCore，直接送 VK 給前景視窗）
# ═══════════════════════════════════════════════════════════
_INPUT_KEYBOARD    = 1
_KEYEVENTF_KEYUP   = 0x0002
_KEYEVENTF_UNICODE = 0x0004

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ('wVk',         ctypes.wintypes.WORD),
        ('wScan',       ctypes.wintypes.WORD),
        ('dwFlags',     ctypes.wintypes.DWORD),
        ('time',        ctypes.wintypes.DWORD),
        ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong)),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [('ki', _KEYBDINPUT), ('_pad', ctypes.c_byte * 28)]

class _INPUT(ctypes.Structure):
    _fields_ = [('type', ctypes.wintypes.DWORD), ('union', _INPUT_UNION)]

def _send_vk(vk: int, scan: int):
    inputs = (_INPUT * 2)()
    for i, flags in enumerate([0, _KEYEVENTF_KEYUP]):
        inputs[i].type = _INPUT_KEYBOARD
        inputs[i].union.ki.wVk   = vk
        inputs[i].union.ki.wScan = scan
        inputs[i].union.ki.dwFlags = flags
    ctypes.windll.user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))


def _send_enter_bypass_ime():
    """同步：暫時解除前景視窗 IME → 送 VK_RETURN → 恢復 IME。
    在 WH_KEYBOARD_LL hook callback 中直接呼叫（同步），
    確保 IME 在 Enter 送出前已解除，不會產生注音組字框。
    """
    _IACE_DEFAULT         = 0x01
    _IACE_IGNORENOCONTEXT = 0x10
    imm32 = ctypes.windll.imm32
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if hwnd:
        imm32.ImmAssociateContextEx(hwnd, None, _IACE_IGNORENOCONTEXT)
    # 送 VK_RETURN
    inputs = (_INPUT * 2)()
    inputs[0].type = _INPUT_KEYBOARD
    inputs[0].union.ki.wVk   = 0x0D   # VK_RETURN
    inputs[0].union.ki.wScan = 0x1C
    inputs[0].union.ki.dwFlags = 0    # 普通按下（IME 已解除，不會組字）
    inputs[1].type = _INPUT_KEYBOARD
    inputs[1].union.ki.wVk   = 0x0D
    inputs[1].union.ki.wScan = 0x1C
    inputs[1].union.ki.dwFlags = _KEYEVENTF_KEYUP
    user32.SendInput(2, inputs, ctypes.sizeof(_INPUT))
    # 恢復 IME
    if hwnd:
        imm32.ImmAssociateContextEx(hwnd, None, _IACE_DEFAULT)


def _send_unicode_chars(text: str):
    """用 KEYEVENTF_UNICODE 直接送 Unicode 字元給前景視窗，完全繞過 IME。
    wVk=0, wScan=codepoint，搭配 KEYEVENTF_UNICODE。
    注意：BMP 以外的字元需拆成 surrogate pair，但點字輸出都在 BMP 內，不需處理。
    """
    if not text:
        return
    n = len(text)
    inputs = (_INPUT * (n * 2))()
    idx = 0
    for ch in text:
        code = ord(ch)
        for flags in (0, _KEYEVENTF_KEYUP):
            inputs[idx].type = _INPUT_KEYBOARD
            inputs[idx].union.ki.wVk    = 0
            inputs[idx].union.ki.wScan  = code
            inputs[idx].union.ki.dwFlags = _KEYEVENTF_UNICODE | flags
            idx += 1
    ctypes.windll.user32.SendInput(n * 2, inputs, ctypes.sizeof(_INPUT))


# ═══════════════════════════════════════════════════════════
# Phn.tbl 載入
# ═══════════════════════════════════════════════════════════
def _load_phn_tbl(path):
    result = {}
    try:
        raw = open(path, 'rb').read().decode('utf-8', errors='replace')
        raw = raw.replace('\x1a','').replace('\r\n','\n').replace('\r','\n')
        lines = [l for l in raw.split('\n') if l]
        for i in range(0, len(lines)-2, 3):
            brl, key, mask = lines[i], lines[i+1], lines[i+2]
            if brl:
                result[brl] = (key, mask)
    except Exception:
        log.error("6d-IME: 無法載入 Phn.tbl", exc_info=True)
    return result


# ═══════════════════════════════════════════════════════════
# 偏好設定對話框
# ═══════════════════════════════════════════════════════════
class TogglePrefsDialog(wx.Dialog):
    """選擇 toggle 循環中包含哪些模式（含一般輸入法）"""

    def __init__(self, parent, enabled_modes):
        super().__init__(parent, title='六點輸入法：模式選擇',
                         style=wx.DEFAULT_DIALOG_STYLE)
        self._result = list(enabled_modes)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label='選擇 toggle 循環中包含的模式（至少兩個）：')
        sizer.Add(label, 0, wx.ALL, 8)
        self._checks = {}
        for mode in _ALL_MODES_WITH_OFF:
            cb = wx.CheckBox(self, label=_MODE_NAMES[mode])
            cb.SetValue(mode in enabled_modes)
            self._checks[mode] = cb
            sizer.Add(cb, 0, wx.LEFT | wx.RIGHT, 16)
        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(self, wx.ID_OK)
        ok_btn.SetDefault()
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(wx.Button(self, wx.ID_CANCEL))
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)
        sizer.Fit(self)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, evt):
        selected = [m for m, cb in self._checks.items() if cb.GetValue()]
        if len(selected) < 2:
            wx.MessageBox('請至少選擇兩個模式。', '提示', wx.OK | wx.ICON_WARNING, self)
            return
        self._result = selected
        self.EndModal(wx.ID_OK)

    @property
    def result(self):
        return self._result


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    """
    6d-IME：六點點字多模式輸入

    注音標點輸入：
      雙格序列（如⠒⠒=：）→ 輸入第二格立即送出
      單格+空白（如⠕+空白=））→ 空白鍵確認
    """

    def __init__(self):
        super().__init__()
        plugin_dir = os.path.dirname(__file__)
        self._phn = _load_phn_tbl(os.path.join(plugin_dir, 'Phn.tbl'))
        log.info(f"6d-IME v10l: Phn.tbl {len(self._phn)} 筆")
        log.debug(f"6d-IME: 立即標點集合: {_PUNCT_IMMEDIATE}")

        self._mode            = None
        self._saved_brl_table = None
        self._ime_is_chinese  = None   # None=未知, True=中文, False=英數

        self._dots_down     = 0
        self._dots_snapshot = 0
        self._dots_any      = False
        self._space_down       = False   # 空白鍵目前是否按下

        # 注音緩衝
        self._brl_buf   = ''
        # 標點序列緩衝（只在 _brl_buf 空時使用）
        self._punct_buf = []
        # 剛送出音節的標誌：True 時 bits=1(f) 解讀為輕聲，False 時解讀為ㄓ前綴
        self._just_sent = False

        # 方案B：英文模式點字緩衝與 liblouis table 路徑
        # 用來在完全不觸發 IME 的情況下自行翻譯並送出文字
        # _eng_buf：int list，存 raw bits（對應 bufferBraille）
        # _eng_buf_text：已翻譯並送出的文字（對應 bufferText）
        # _eng_contracted：目前 table 是否為縮寫表（決定上字時機）
        self._eng_buf        = []   # List[int]
        self._eng_buf_text   = ''
        self._eng_contracted = False
        self._eng_table_path = []   # 傳給 louis.backTranslate 的 table 路徑清單

        # 偏好：toggle 循環包含的模式（預設全部，含一般輸入法）
        self._enabled_modes: list = list(_ALL_MODES_WITH_OFF)

        self._old_kd = winInputHook.keyDownCallback
        self._old_ku = winInputHook.keyUpCallback
        winInputHook.keyDownCallback = self._on_key_down
        winInputHook.keyUpCallback   = self._on_key_up

    def terminate(self):
        winInputHook.keyDownCallback = self._old_kd
        winInputHook.keyUpCallback   = self._old_ku
        self._restore_brl_table()

    # ── 模式切換 ──────────────────────────────────────────

    def _next_mode(self, reverse: bool = False):
        """計算下一個（或上一個）模式，回傳 mode 常數或 None（一般輸入法）。
        _enabled_modes 中可包含 None，直接在清單中循環（頭尾相接）。"""
        modes = self._enabled_modes
        if not modes:
            return None
        if self._mode not in modes:
            # 目前模式不在清單中（例如剛開啟插件）：正向→第一個；反向→最後一個
            return modes[-1] if reverse else modes[0]
        idx = modes.index(self._mode)
        if reverse:
            return modes[idx - 1]           # idx==0 時取 modes[-1]（最後一個）
        else:
            return modes[(idx + 1) % len(modes)]

    def script_toggle(self, gesture):
        """正向循環切換輸入模式"""
        self._set_mode(self._next_mode(reverse=False))

    script_toggle.category = '六點輸入法'

    def script_toggle_reverse(self, gesture):
        """反向循環切換輸入模式"""
        self._set_mode(self._next_mode(reverse=True))

    script_toggle_reverse.category = '六點輸入法'

    def script_prefs(self, gesture):
        """開啟模式選擇偏好對話框"""
        def _show():
            dlg = TogglePrefsDialog(gui.mainFrame, self._enabled_modes)
            if dlg.ShowModal() == wx.ID_OK:
                self._enabled_modes = sorted(dlg.result, key=_ALL_MODES_WITH_OFF.index)
                names = '、'.join(_MODE_NAMES[m] for m in self._enabled_modes)
                ui.message(f'已選擇：{names}')
            dlg.Destroy()
        wx.CallAfter(_show)

    script_prefs.category = '六點輸入法'

    def script_display_dots(self, gesture):
        """攔截點字顯示器的格輸入（bk:dots），路由到對應處理函式。
        支援：純格、純空白、空白+格（組合功能）。
        一般輸入法模式下只處理 space+dots 組合（toggle 觸發），其他放行。
        """
        dots = getattr(gesture, 'dots', 0)
        space = getattr(gesture, 'space', False)
        if self._mode is None:
            # 一般輸入法模式：放行給 NVDA 原生處理
            import scriptHandler
            import globalCommands
            scriptHandler.queueScript(globalCommands.commands.script_braille_dots, gesture)
            return
        if space and not dots:
            queueHandler.queueFunction(queueHandler.eventQueue, self._process_space)
        elif dots:
            queueHandler.queueFunction(queueHandler.eventQueue, self._process_cell, dots)

    script_display_dots.category = '六點輸入法'

    __gestures = {
        'kb:NVDA+control+7':       'toggle',
        'kb:NVDA+control+shift+7': 'toggle_reverse',
        'kb:NVDA+control+8':       'prefs',
        'bk:dots':                 'display_dots',   # 攔截點字顯示器格輸入
    }

    def _set_mode(self, mode):
        old = self._mode
        log.debug(f"6d-IME _set_mode: {old!r} → {mode!r}")
        self._reset_state()
        # 切換時清 brailleInput handler 緩衝
        self._clear_handler_buf()

        if mode is None:
            # 無條件恢復中文 IME
            self._ime_set(chinese=True)
            self._restore_brl_table()
            self._mode = None
            ui.message(_MODE_NAMES[None])
            return

        if mode in _MODE_TABLES:
            try:
                new_tbl = getBRLtable(_MODE_TABLES[mode])
            except Exception:
                log.error(f"6d-IME: 找不到 {_MODE_TABLES[mode]}", exc_info=True)
                ui.message(f'找不到點字表 {_MODE_TABLES[mode]}')
                # 失敗時恢復 IME 到中文
                self._ime_set(chinese=True)
                return
            # 方案B：記錄 liblouis table 路徑供 backTranslate 使用
            # new_tbl.fileName 是 liblouis 可接受的路徑字串
            self._eng_table_path = [new_tbl.fileName]
            self._eng_contracted = new_tbl.contracted
            log.debug(f'6d-IME: eng table path = {self._eng_table_path} contracted={self._eng_contracted}')
            # 無條件切英數 IME，並儲存 braille table（只在非英文模式時儲存）
            if old not in _ENG_MODES:
                self._saved_brl_table = brailleInput.handler.table
            self._ime_set(chinese=False)   # 無條件設定（方案A 仍保留，雙保險）
            brailleInput.handler.table = new_tbl
        elif mode == MODE_BRL_UNICODE:
            # 無條件切中文 IME
            self._ime_set(chinese=True)
            self._restore_brl_table()
        else:  # BOPOMOFO
            # 無條件切中文 IME
            self._ime_set(chinese=True)
            self._restore_brl_table()

        self._mode = mode
        ui.message(_MODE_NAMES[mode])

    def _clear_handler_buf(self):
        try:
            brailleInput.handler._BrailleInputHandler__brailleBuffer = ''
        except Exception:
            pass

    # ── IME 解除關聯（ImmAssociateContextEx）────────────
    # 64 位元 NVDA 2026.1 下，WH_KEYBOARD_LL hook return False
    # 對 IME 無效：IME 仍然攔截按鍵產生組字框。
    # 解法：切到 comp8/UEB 時用 ImmAssociateContextEx 解除
    # 前景視窗的 IME 關聯，IME 完全不再攔截任何按鍵。
    # 切回注音時用 IACE_DEFAULT 恢復。
    # 視窗切換時需重新套用（_ime_ensure 負責）。

    # ImmAssociateContextEx flags
    _IACE_DEFAULT         = 0x01  # 恢復預設 IME context
    _IACE_IGNORENOCONTEXT = 0x10  # 解除時忽略無 context 的視窗

    def _ime_set(self, chinese: bool):
        """立即設定 IME 關聯狀態。"""
        self._ime_is_chinese = chinese
        self._ime_apply()

    def _ime_apply(self):
        """對目前前景視窗套用 IME 關聯狀態。"""
        try:
            imm32 = ctypes.windll.imm32
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return
            if self._ime_is_chinese:
                # 恢復 IME
                ret = imm32.ImmAssociateContextEx(hwnd, None, self._IACE_DEFAULT)
                log.debug(f'6d-IME: IME 恢復 hwnd={hwnd:#x} ret={ret}')
            else:
                # 解除 IME（IACE_IGNORENOCONTEXT 讓解除更強制）
                ret = imm32.ImmAssociateContextEx(hwnd, None, self._IACE_IGNORENOCONTEXT)
                log.debug(f'6d-IME: IME 解除 hwnd={hwnd:#x} ret={ret}')
        except Exception:
            log.debug('6d-IME: _ime_apply 失敗', exc_info=True)

    def _ime_ensure(self, chinese: bool):
        """每格輸入時確認狀態，視窗切換後補正。"""
        if self._ime_is_chinese != chinese:
            self._ime_is_chinese = chinese
            self._ime_apply()

    def _clear_key_state(self, vkCode: int):
        """用 SetKeyboardState 把指定 VK 的按下狀態清為 0，
        讓 IME 的 ToUnicodeEx 無法轉換出字元，阻止注音組字。
        在 64 位元 NVDA 下 hook return False 無法阻止 IME，
        必須額外用此方法讓 IME 找不到字元可組。"""
        try:
            ks = (ctypes.c_byte * 256)()
            ctypes.windll.user32.GetKeyboardState(ks)
            ks[vkCode] = 0          # 清除此鍵的按下狀態
            ctypes.windll.user32.SetKeyboardState(ks)
        except Exception:
            pass

    def _restore_brl_table(self):
        if self._saved_brl_table is not None:
            try:
                brailleInput.handler.table = self._saved_brl_table
            except Exception:
                pass
            self._saved_brl_table = None

    def _reset_state(self):
        self._dots_down     = 0
        self._dots_snapshot = 0
        self._dots_any      = False
        self._space_down    = False
        self._brl_buf       = ''
        self._punct_buf     = []
        self._just_sent     = False
        self._eng_buf       = []
        self._eng_buf_text  = ''
        # 清除點字顯示器的 untranslated 狀態
        try:
            brailleInput.handler.untranslatedBraille = ''
            brailleInput.handler.updateDisplay()
        except Exception:
            pass

    # ── 鍵盤 Hook ─────────────────────────────────────────

    def _on_key_down(self, vkCode, scanCode, extended, injected):
        if injected:
            return self._old_kd(vkCode, scanCode, extended, injected)
        # 有修飾鍵（Ctrl / Alt / Win）時完全放行，不干擾快捷鍵
        ks = (ctypes.c_byte * 256)()
        ctypes.windll.user32.GetKeyboardState(ks)
        if ks[0x11] & 0x80 or ks[0x12] & 0x80 or ks[0x5B] & 0x80 or ks[0x5C] & 0x80:
            return self._old_kd(vkCode, scanCode, extended, injected)
        # VK_SPACE：有模式時攔截；一般輸入法時完全放行給 IME
        if vkCode == VK_SPACE:
            if self._mode is not None:
                self._space_down = True
                self._clear_key_state(vkCode)
                return False
            return self._old_kd(vkCode, scanCode, extended, injected)
        if vkCode == 0x41:   # A = 點7
            if self._mode is not None:
                self._dots_down    |= (1 << 6)
                self._dots_snapshot = self._dots_down
                self._dots_any      = True
                self._clear_key_state(vkCode)
                return False
        if vkCode == 0x3B:   # ; = 點8（所有模式下 Enter）
            if self._mode is not None:
                # 所有模式：keydown 時同步解除 IME → 送 Enter → 恢復 IME
                # 不走 bits/keyup 機制，避免 IME 在 hook 之前吃掉 ; 產生 ㄤ
                _send_enter_bypass_ime()
                return False
        # 六點鍵（fdsjkl）：有模式時攔截；一般輸入法時完全放行給 IME
        if vkCode in _BRL_KEY_VK:
            if self._mode is not None:
                self._dots_down    |= _BRL_KEY_VK[vkCode]
                self._dots_snapshot = self._dots_down
                self._dots_any      = True
                self._clear_key_state(vkCode)
                return False
            return self._old_kd(vkCode, scanCode, extended, injected)
        if self._mode is None:
            # 一般輸入法模式：其他鍵放行
            return self._old_kd(vkCode, scanCode, extended, injected)
        # 其他鍵（輸入模式下）：清狀態，放行
        if self._brl_buf or self._punct_buf:
            self._error_beep()
            self._brl_buf   = ''
            self._punct_buf = []
        return self._old_kd(vkCode, scanCode, extended, injected)

    def _on_key_up(self, vkCode, scanCode, extended, injected):
        if injected:
            return self._old_ku(vkCode, scanCode, extended, injected)
        # 有修飾鍵時完全放行
        ks = (ctypes.c_byte * 256)()
        ctypes.windll.user32.GetKeyboardState(ks)
        if ks[0x11] & 0x80 or ks[0x12] & 0x80 or ks[0x5B] & 0x80 or ks[0x5C] & 0x80:
            return self._old_ku(vkCode, scanCode, extended, injected)
        # VK_SPACE 放開
        if vkCode == VK_SPACE:
            if self._mode is None:
                return self._old_ku(vkCode, scanCode, extended, injected)
            was_down = self._space_down
            self._space_down = False
            if was_down and not self._dots_any:
                self._process_space()
            return False
        # 點7(A)和點8(;)：mode is None 時只處理 space+dots 組合，否則完整處理
        if vkCode == 0x41:   # A = 點7
            if self._mode is not None:
                self._dots_down &= ~(1 << 6)
                if self._dots_any and self._dots_down == 0:
                    self._dots_any      = False
                    bits                = self._dots_snapshot
                    self._dots_snapshot = 0
                    self._process_cell(bits)
                return False
        if vkCode == 0x3B:   # ; = 點8（Enter 已在 keydown 送出，keyup 直接吸掉）
            if self._mode is not None:
                return False
        if vkCode in _BRL_KEY_VK:
            if self._mode is None:
                return self._old_ku(vkCode, scanCode, extended, injected)
            self._dots_down &= ~_BRL_KEY_VK[vkCode]
            if self._dots_any and self._dots_down == 0:
                self._dots_any      = False
                bits                = self._dots_snapshot
                self._dots_snapshot = 0
                self._process_cell(bits)
            return False
        return self._old_ku(vkCode, scanCode, extended, injected)


    # ── 空白鍵 ────────────────────────────────────────────

    def _process_space(self):
        if self._mode in _ENG_MODES:
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                self._eng_flush_and_space)
            return

        if self._mode == MODE_BRL_UNICODE:
            # 輸出普通空格（不送 ⠀，使文字編輯器得到正確的分詞空格）
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                _send_unicode_chars, ' ')
            return

        # ── 注音模式 ──
        if self._brl_buf:
            # 有音節 → 第1聲確認
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                _send_vk, *_DAQIAN_KEY[' '])
            self._brl_buf   = ''
            self._punct_buf = []
            return

        if self._punct_buf:
            # 有標點序列 → 查表送出
            key = tuple(self._punct_buf)
            self._punct_buf = []
            punct = _PUNCT_SEQ.get(key)
            if punct:
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    brailleInput.handler.sendChars, punct)
            else:
                # 不是標點 → 逐格轉注音，再送第1聲確認
                for b in key:
                    self._bopo_inner(b)
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    _send_vk, *_DAQIAN_KEY[' '])
            return

        # 完全空白 → 輸出空格（sendChars 繞過 IME 的 space 特殊行為）
        queueHandler.queueFunction(
            queueHandler.eventQueue,
            brailleInput.handler.sendChars, ' ')

    # ── 格處理（模式分派）─────────────────────────────────

    def _process_cell(self, bits: int):
        if bits == 0:
            return
        # 點7單獨 = Backspace，點8單獨 = Enter（所有模式均適用）
        if bits == 64:    # 點7
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                _send_vk, 0x08, 0x0E)   # VK_BACK, scan=0x0E
            return
        if bits == 128:   # 點8 → Enter（用 KEYEVENTF_UNICODE 繞過中文 IME）
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                _send_unicode_chars, chr(13))
            return
        if self._mode in _ENG_MODES:
            # 方案B：不走 brailleInput.handler，改用自己的緩衝 + KEYEVENTF_UNICODE
            # 不呼叫任何 SendInput VK，IME 根本收不到按鍵，不會產生組字框
            if bits > 255:
                return
            self._eng_buf.append(bits)
            log.debug(f'6d-IME: eng_buf append bits={bits:#04x}  contracted={self._eng_contracted}')
            if not self._eng_contracted:
                # 非縮寫表（comp8、UEB G1）：每格立即翻譯上字，行為同 brailleInput.handler
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    self._eng_translate, False)
            else:
                # 縮寫表（UEB G2）：累積到空白鍵再 flush（_process_space 負責）
                # 語音回饋：報讀點字格
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    self._eng_report_cell, bits)
            return
        elif self._mode == MODE_BRL_UNICODE:
            # 直接輸出 Unicode 點字字元 U+2800+bits
            # 使用 KEYEVENTF_UNICODE 繞過 IME（sendChars 在中文 IME 下會被攔截）
            if bits > 255:
                return
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                _send_unicode_chars, chr(0x2800 + bits))
        else:
            # 注音模式：確保 IME 為中文，只用 6-dot（bits 1-63）
            self._ime_ensure(chinese=True)
            if bits >= 64:
                return
            self._process_bopomofo(bits)

    # ── 注音/標點分派 ─────────────────────────────────────

    def _process_bopomofo(self, bits: int):
        """
        路由規則：
        1. 調號格（bits=1,2,4,8,16）：
           先清 punct_buf（轉注音），再處理調號
        2. brl_buf 有內容：繼續注音
        3. brl_buf 空：
           若 punct_buf 能接受 bits（是有效前綴）→ 累積標點
           否則：把 punct_buf 轉注音，再把 bits 送注音
        """
        is_tone = (bits == 1 or bits in _TONE_DIRECT)

        if is_tone:
            # 調號：先把 punct_buf 轉注音
            if self._punct_buf:
                buf = list(self._punct_buf)
                self._punct_buf = []
                for b in buf:
                    self._bopo_inner(b)
            self._bopo_inner(bits)
            return

        if self._brl_buf:
            # 注音進行中：繼續注音
            self._bopo_inner(bits)
            return

        # brl_buf 空：嘗試累積標點序列
        candidate = tuple(self._punct_buf) + (bits,)
        if candidate in _PUNCT_PREFIXES:
            self._punct_buf.append(bits)
            # 若立即命中（雙格且無更長序列）→ 立即送出
            if candidate in _PUNCT_IMMEDIATE:
                self._punct_buf = []
                punct = _PUNCT_SEQ[candidate]
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    brailleInput.handler.sendChars, punct)
            return

        # candidate 不是前綴：把 punct_buf 轉注音，再處理新 bits
        if self._punct_buf:
            buf = list(self._punct_buf)
            self._punct_buf = []
            for b in buf:
                self._bopo_inner(b)
        # 再試一次只有 (bits,)
        if (bits,) in _PUNCT_PREFIXES:
            self._punct_buf.append(bits)
            if (bits,) in _PUNCT_IMMEDIATE:
                self._punct_buf = []
                punct = _PUNCT_SEQ[(bits,)]
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    brailleInput.handler.sendChars, punct)
        else:
            self._bopo_inner(bits)

    # ── 注音邏輯核心 ──────────────────────────────────────

    def _bopo_inner(self, bits: int):
        """
        處理注音格（調號 + 一般格）。
        調號觸發規則：
          A) buf 空 → 直接送調號
          B) buf 在 Phn.tbl 完整命中 → 先送音節再送調號
          C) buf 是純前綴（等韻母）→ 試把 NABCC(bits) 接在 buf 後：
               有效 → 繼續累積；無效 → 錯誤提示清空
        """
        if bits == 1:
            if self._brl_buf:
                # buf 有內容：音節尚未送出，輕聲確認
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    _send_vk, *_DAQIAN_KEY[_BITS1_TONE])
                self._brl_buf   = ''
                self._just_sent = False
            elif self._just_sent:
                # buf 空但剛送出音節：輕聲調號（送大千 '7'）
                self._just_sent = False
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    _send_vk, *_DAQIAN_KEY[_BITS1_TONE])
            else:
                # buf 空且沒有剛送出音節：ㄓ聲母前綴
                self._brl_buf += _NABCC[1]   # 'a'
                self._try_lookup()
            return

        ch = _NABCC[bits]
        td = _TONE_DIRECT.get(bits)

        if td is not None:
            # 調號
            if not self._brl_buf:
                self._just_sent = False
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    _send_vk, *_DAQIAN_KEY[td])
                return
            if self._brl_buf in self._phn:
                # B) 完整命中：先送音節再送調號
                ks = self._phn[self._brl_buf][0]
                self._brl_buf = ''
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    self._send_daqian_seq, ks)
                queueHandler.queueFunction(
                    queueHandler.eventQueue,
                    _send_vk, *_DAQIAN_KEY[td])
                return
            # C) 純前綴：試接
            test = self._brl_buf + ch
            if test in self._phn or any(
                    k.startswith(test) and len(k) > len(test)
                    for k in self._phn):
                self._brl_buf = test
                if test in self._phn:
                    ks = self._phn[test][0]
                    queueHandler.queueFunction(
                        queueHandler.eventQueue,
                        self._send_daqian_seq, ks)
                    self._brl_buf = ''
            else:
                log.warning(f"6d-IME: {repr(self._brl_buf)}+bits={bits} 無效，清空")
                self._error_beep()
                self._brl_buf = ''
            return

        # 一般格
        self._just_sent = False
        self._brl_buf += ch
        self._try_lookup()

    def _try_lookup(self):
        buf = self._brl_buf
        if buf in self._phn:
            ks = self._phn[buf][0]
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                self._send_daqian_seq, ks)
            self._brl_buf  = ''
            self._just_sent = True   # 剛送出音節，下一個 bits=1 是輕聲而非ㄓ前綴
            return
        if len(buf) == 1:
            return
        first = buf[0]
        if first in self._phn:
            ks = self._phn[first][0]
            queueHandler.queueFunction(
                queueHandler.eventQueue,
                self._send_daqian_seq, ks)
            self._brl_buf = buf[1:]
            self._try_lookup()
        else:
            log.warning(f"6d-IME: {repr(buf)} 無法解析")
            self._error_beep()
            self._brl_buf = ''

    # ── 送出 ──────────────────────────────────────────────

    def _eng_translate(self, end_word: bool):
        """方案B 核心翻譯函式，完整複製 brailleInput._translate 的邏輯。

        使用 dotsIO 模式（cell | 0x8000）搭配 louis.backTranslate，
        與 NVDA 原生行為完全一致。翻譯出的新文字用 KEYEVENTF_UNICODE 送出。
        翻譯後同步更新 brailleInput.handler.untranslatedBraille 與 updateDisplay()，
        使點字顯示器顯示正在輸入的未送出格（與 NVDA 原生輸入相同的視覺回饋）。

        @param end_word: True 表示空白鍵觸發（清緩衝），False 表示一般格輸入（非縮寫表用）。
        """
        if not self._eng_buf:
            if end_word:
                _send_unicode_chars(' ')
            return
        if not self._eng_table_path:
            log.warning('6d-IME: _eng_translate 呼叫時 eng_table_path 為空')
            return

        LOUIS_DOTS_IO_START = 0x8000
        data = ''.join(chr(cell | LOUIS_DOTS_IO_START) for cell in self._eng_buf)
        mode = louis.dotsIO | louis.noUndefinedDots
        try:
            result = louis.backTranslate(
                self._eng_table_path + ['braille-patterns.cti'],
                data,
                mode=mode,
            )
            full_text = result[0] if isinstance(result, (tuple, list)) else result
        except Exception:
            log.debug('6d-IME: louis.backTranslate 失敗', exc_info=True)
            self._error_beep()
            return

        log.debug(f'6d-IME: _eng_translate end_word={end_word} full={full_text!r}')

        if end_word:
            if self._eng_contracted:
                # 縮寫表（G2）end_word：各格沒有即時送出，現在一次送整個單字
                if full_text:
                    _send_unicode_chars(full_text)
            # 非縮寫表（comp8/G1）end_word：各格已即時送出，不重複送，只清緩衝送空格
            self._eng_buf      = []
            self._eng_buf_text = ''
            self._eng_sync_display(untranslated_bits=[])
            _send_unicode_chars(' ')
        else:
            # 非縮寫表每格：增量翻譯，只送出比上次多的部分
            old_text_len = len(self._eng_buf_text)
            self._eng_buf_text = full_text
            new_text = full_text[old_text_len:]
            if new_text:
                _send_unicode_chars(new_text)
                self._eng_sync_display(untranslated_bits=[])
            else:
                # 沒有產生文字（如數字符號 number sign）：報讀點字格
                import config as _config
                if _config.conf["keyboard"]["speakTypedCharacters"]:
                    brailleInput.speakDots(self._eng_buf[-1])
                self._eng_sync_display(untranslated_bits=self._eng_buf)

    def _eng_report_cell(self, bits: int):
        """縮寫表（UEB G2）每格的語音＋顯示回饋。
        完整複製 brailleInput._reportUntranslated + _reportContractedCell 的邏輯：
        用 partialTrans 模式翻譯目前整個 buf，取出比上次多出的文字部分，
        唸出對應字母或單字（例如 ⠪ → "o w"，⠐⠅ → "k n o w"）。
        若沒有產生文字（如數字符號），才 fallback 到 speakDots。
        """
        import config as _config
        if not _config.conf["keyboard"]["speakTypedCharacters"]:
            # 使用者關閉「輸入時唸出字元」：只更新顯示，不語音
            self._eng_sync_display(untranslated_bits=self._eng_buf)
            return

        reported = self._eng_report_contracted_cell()
        if not reported:
            brailleInput.speakDots(bits)
        self._eng_sync_display(untranslated_bits=self._eng_buf)

    def _eng_report_contracted_cell(self) -> bool:
        """對目前 _eng_buf 做 partialTrans 翻譯，取出新增文字並語音播報。
        複製 brailleInput._reportContractedCell 的核心邏輯，
        但完全使用插件自己的 _eng_buf / _eng_buf_text，不碰 handler 的內部狀態。

        @return: True 若成功播報文字，False 若沒有新文字（需 fallback 到 speakDots）。
        """
        if not self._eng_table_path or not self._eng_buf:
            return False
        LOUIS_DOTS_IO_START = 0x8000
        data = ''.join(chr(cell | LOUIS_DOTS_IO_START) for cell in self._eng_buf)
        try:
            result = louis.backTranslate(
                self._eng_table_path + ['braille-patterns.cti'],
                data,
                mode=louis.dotsIO | louis.noUndefinedDots | louis.partialTrans,
            )
            new_full_text = result[0] if isinstance(result, (tuple, list)) else result
        except Exception:
            log.debug('6d-IME: _eng_report_contracted_cell backTranslate 失敗', exc_info=True)
            return False

        old_text_len = len(self._eng_buf_text)

        # 若前面的文字被這格改變了（縮寫擴展），不做猜測（與 NVDA 行為一致）
        if self._eng_buf_text and new_full_text[:old_text_len] != self._eng_buf_text:
            # 前段文字已改變：更新 buf_text 但不播報
            self._eng_buf_text = new_full_text
            return False

        new_text = new_full_text[old_text_len:]
        # 更新 partial 翻譯結果到 buf_text（供下一格做增量比對）
        self._eng_buf_text = new_full_text

        if new_text:
            # 逐字元空格分隔播報，例如 "o w" 或 "know"
            speech.speakMessage(' '.join(new_text))
            return True
        return False

    def _eng_sync_display(self, untranslated_bits: list):
        """把 untranslated_bits 寫入 brailleInput.handler.untranslatedBraille，
        並觸發 updateDisplay()，讓點字顯示器顯示正在輸入的點字格。

        untranslated_bits 為空時清除顯示（送出後或清緩衝後）。
        """
        try:
            brailleInput.handler.untranslatedBraille = ''.join(
                chr(0x2800 + b) for b in untranslated_bits
            )
            brailleInput.handler.updateDisplay()
        except Exception:
            log.debug('6d-IME: _eng_sync_display 失敗', exc_info=True)

    def _eng_flush_and_space(self):
        """縮寫表模式：空白鍵時翻譯整個緩衝並送出，再送空格。"""
        self._eng_translate(True)

    def _send_daqian_seq(self, seq: str):
        for ch in seq:
            pair = _DAQIAN_KEY.get(ch)
            if pair:
                _send_vk(*pair)

    def _error_beep(self):
        try:
            winsound.Beep(440, 80)
        except Exception:
            pass
