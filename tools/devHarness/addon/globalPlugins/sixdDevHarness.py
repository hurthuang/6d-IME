# 6d-IME 開發測試用附加元件（不要發佈）
# 用 %TEMP%\sixdDevHarness\ 的 req.json／resp.json 交換指令，
# wx.Timer 在 NVDA 主執行緒執行（TSF 是 STA COM，必須在主執行緒呼叫）。

import ctypes
import json
import os
from ctypes import wintypes, POINTER, byref, Structure, c_ushort

import comtypes
import comtypes.client
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT
import globalPluginHandler
import inputCore
import braille
import brailleInput
import NVDAHelper
import wx
from logHandler import log

user32 = ctypes.windll.user32

# ── TSF 定義（只取需要的部分，vtable 順序照 msctf.h）──────────

TF_PROFILETYPE_INPUTPROCESSOR = 1
GUID_TFCAT_TIP_KEYBOARD = GUID("{34745C63-B2F0-4784-8B67-5E12C8701A31}")
CLSID_TF_InputProcessorProfiles = GUID("{33C53A50-F456-4884-B049-85FD643ECFED}")
MS_BOPOMOFO_CLSID = GUID("{B115690A-EA02-48D5-A231-E3578D2FDF80}")
MS_BOPOMOFO_PROFILE = GUID("{B2F9C502-1742-11D4-9790-0080C882687E}")

FLAGS = {
	"ENABLEPROFILE": 0x00000001,
	"DONTCARECURRENTINPUTLANGUAGE": 0x00000004,
	"FORPROCESS": 0x10000000,
	"FORSESSION": 0x20000000,
	"FORSYSTEMALL": 0x40000000,
}


class TF_INPUTPROCESSORPROFILE(Structure):
	_fields_ = [
		("dwProfileType", wintypes.DWORD),
		("langid", c_ushort),
		("clsid", GUID),
		("guidProfile", GUID),
		("catid", GUID),
		("hklSubstitute", wintypes.HKL),
		("dwCaps", wintypes.DWORD),
		("hkl", wintypes.HKL),
		("dwFlags", wintypes.DWORD),
	]


class IEnumTfInputProcessorProfiles(IUnknown):
	_iid_ = GUID("{71C6E74D-0F28-11D8-A82A-00065B84435C}")
	_methods_ = [
		COMMETHOD([], HRESULT, "Clone", (["out"], POINTER(POINTER(IUnknown)), "ppEnum")),
		COMMETHOD([], HRESULT, "Next",
			(["in"], wintypes.ULONG, "ulCount"),
			(["in"], POINTER(TF_INPUTPROCESSORPROFILE), "pProfile"),
			(["in"], POINTER(wintypes.ULONG), "pcFetch")),
		COMMETHOD([], HRESULT, "Reset"),
		COMMETHOD([], HRESULT, "Skip", (["in"], wintypes.ULONG, "ulCount")),
	]


class ITfInputProcessorProfileMgr(IUnknown):
	_iid_ = GUID("{71C6E74C-0F28-11D8-A82A-00065B84435C}")
	_methods_ = [
		COMMETHOD([], HRESULT, "ActivateProfile",
			(["in"], wintypes.DWORD, "dwProfileType"),
			(["in"], c_ushort, "langid"),
			(["in"], POINTER(GUID), "clsid"),
			(["in"], POINTER(GUID), "guidProfile"),
			(["in"], wintypes.HKL, "hkl"),
			(["in"], wintypes.DWORD, "dwFlags")),
		COMMETHOD([], HRESULT, "DeactivateProfile",
			(["in"], wintypes.DWORD, "dwProfileType"),
			(["in"], c_ushort, "langid"),
			(["in"], POINTER(GUID), "clsid"),
			(["in"], POINTER(GUID), "guidProfile"),
			(["in"], wintypes.HKL, "hkl"),
			(["in"], wintypes.DWORD, "dwFlags")),
		COMMETHOD([], HRESULT, "GetProfile",
			(["in"], wintypes.DWORD, "dwProfileType"),
			(["in"], c_ushort, "langid"),
			(["in"], POINTER(GUID), "clsid"),
			(["in"], POINTER(GUID), "guidProfile"),
			(["in"], wintypes.HKL, "hkl"),
			(["in"], POINTER(TF_INPUTPROCESSORPROFILE), "pProfile")),
		COMMETHOD([], HRESULT, "EnumProfiles",
			(["in"], c_ushort, "langid"),
			(["out"], POINTER(POINTER(IEnumTfInputProcessorProfiles)), "ppEnum")),
		# 以下只佔 vtable 位置，不會呼叫
		COMMETHOD([], HRESULT, "ReleaseInputProcessor"),
		COMMETHOD([], HRESULT, "RegisterProfile"),
		COMMETHOD([], HRESULT, "UnregisterProfile"),
		COMMETHOD([], HRESULT, "GetActiveProfile",
			(["in"], POINTER(GUID), "catid"),
			(["in"], POINTER(TF_INPUTPROCESSORPROFILE), "pProfile")),
	]


def _mgr():
	return comtypes.client.CreateObject(CLSID_TF_InputProcessorProfiles, interface=ITfInputProcessorProfileMgr)


def _profile_dict(p):
	return {
		"langid": "%04X" % p.langid,
		"clsid": str(p.clsid),
		"profile": str(p.guidProfile),
		"hkl": "%08X" % (p.hkl or 0),
		"flags": p.dwFlags,  # 2 = TF_IPP_FLAG_ACTIVE（對呼叫端執行緒而言）
	}


# ── 指令 ────────────────────────────────────────────

def cmd_state(args):
	hwnd = user32.GetForegroundWindow()
	pid = wintypes.DWORD()
	tid = user32.GetWindowThreadProcessId(hwnd, byref(pid))
	title = ctypes.create_unicode_buffer(256)
	user32.GetWindowTextW(hwnd, title, 256)
	hkl = user32.GetKeyboardLayout(tid) & 0xFFFFFFFF
	active = None
	try:
		p = TF_INPUTPROCESSORPROFILE()
		_mgr().GetActiveProfile(byref(GUID_TFCAT_TIP_KEYBOARD), byref(p))
		active = _profile_dict(p)
	except Exception as e:
		active = "error: %r" % e
	return {
		"hwnd": hwnd, "pid": pid.value, "tid": tid, "title": title.value,
		"fgHKL": "%08X" % hkl,
		"nvdaLastLayoutString": getattr(NVDAHelper, "lastLayoutString", None),
		"nvdaLastLanguageID": getattr(NVDAHelper, "lastLanguageID", None),
		"nvdaThreadActiveProfile": active,
	}


def cmd_profiles(args):
	lang = int(args.get("langid", "0404"), 16)
	enum = _mgr().EnumProfiles(lang)
	out = []
	while True:
		p = TF_INPUTPROCESSORPROFILE()
		n = wintypes.ULONG()
		enum.Next(1, byref(p), byref(n))
		if n.value == 0:
			break
		out.append(_profile_dict(p))
	return out


def cmd_activate(args):
	clsid = GUID(args.get("clsid", str(MS_BOPOMOFO_CLSID)))
	prof = GUID(args.get("profile", str(MS_BOPOMOFO_PROFILE)))
	flags = 0
	for name in args.get("flags", []):
		flags |= FLAGS[name]
	hr = _mgr().ActivateProfile(TF_PROFILETYPE_INPUTPROCESSOR, 0x0404, byref(clsid), byref(prof), 0, flags)
	return {"hr": hr, "flags": "%08X" % flags}


def cmd_dots(args):
	"""模擬點字顯示器手勢，走 NVDA 一般的手勢處理流程。"""
	class _Gesture(braille.BrailleDisplayGesture, brailleInput.BrailleInputGesture):
		source = args.get("source", "freedomScientific")
		model = args.get("model", "Focus40")
	g = _Gesture()
	g.dots = int(args.get("dots", 0))
	g.space = bool(args.get("space", False))
	keys = ["dot%d" % (i + 1) for i in range(8) if g.dots & (1 << i)]
	if g.space:
		keys.append("brailleSpaceBar")
	g.id = "+".join(keys)
	try:
		inputCore.manager.emulateGesture(g)
		return {"sent": g.id, "identifiers": list(g.identifiers)}
	except inputCore.NoInputGestureAction:
		return {"sent": g.id, "identifiers": list(g.identifiers), "noAction": True}


def cmd_ids(args):
	"""只算點字手勢的 identifiers，不執行。參數同 dots。"""
	class _Gesture(braille.BrailleDisplayGesture, brailleInput.BrailleInputGesture):
		source = args.get("source", "freedomScientific")
		model = args.get("model", "Focus40")
	g = _Gesture()
	g.dots = int(args.get("dots", 0))
	g.space = bool(args.get("space", False))
	keys = ["dot%d" % (i + 1) for i in range(8) if g.dots & (1 << i)]
	if g.space:
		keys.append("brailleSpaceBar")
	g.id = "+".join(keys)
	return list(g.identifiers)


def cmd_profile_names(args):
	"""每個輸入法的顯示名稱（GetLanguageProfileDescription）。"""
	import comtypes.client as cc
	out = []
	for p in cmd_profiles(args):
		if p["clsid"].startswith("{00000000"):
			continue
		out.append({**p, "name": _profile_name(p["clsid"], p["profile"], int(p["langid"], 16))})
	return out


def _profile_name(clsid, profile, langid):
	# ITfInputProcessorProfiles::GetLanguageProfileDescription 用 vtable 直接呼叫（第 13 個方法）
	try:
		import winreg
		key = r"SOFTWARE\Microsoft\CTF\TIP\%s\LanguageProfile\0x%08x\%s" % (clsid, langid, profile)
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
			return winreg.QueryValueEx(k, "Description")[0]
	except Exception as e:
		return "?"


# 記錄 NVDA 念出的內容（給測試比對用）
_spoken = []


_trace = {"pattern": None, "stacks": []}


def _on_pre_speech(speechSequence=None, **kw):
	try:
		text = " ".join(s for s in (speechSequence or []) if isinstance(s, str))
		_spoken.append(text)
		del _spoken[:-200]
		if _trace["pattern"] and _trace["pattern"] in text:
			import traceback
			_trace["stacks"].append(text + "\n" + "".join(traceback.format_stack(limit=25)))
	except Exception:
		pass


def cmd_trace(args):
	"""{"pattern": "..."} 設定要記錄堆疊的語音；不給 pattern 時回傳並清除記錄。"""
	if "pattern" in args:
		_trace["pattern"] = args["pattern"]
		_trace["stacks"] = []
		return {"ok": True}
	out = list(_trace["stacks"])
	_trace["stacks"] = []
	return out


def cmd_spoken(args):
	out = list(_spoken)
	if args.get("clear", True):
		_spoken.clear()
	return out


def _find_window(args):
	found = []

	@ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
	def cb(hwnd, lp):
		if not user32.IsWindowVisible(hwnd):
			return True
		buf = ctypes.create_unicode_buffer(256)
		user32.GetClassNameW(hwnd, buf, 256)
		cls = buf.value
		user32.GetWindowTextW(hwnd, buf, 256)
		if args.get("cls") in (None, cls) and args.get("title", "") in buf.value:
			found.append(hwnd)
		return True
	user32.EnumWindows(cb, 0)
	return found


def cmd_focus(args):
	hwnds = _find_window(args)
	if not hwnds:
		return {"error": "window not found"}
	hwnd = hwnds[0]
	user32.ShowWindow(hwnd, 9)  # SW_RESTORE
	ok = user32.SetForegroundWindow(hwnd)
	return {"hwnd": hwnd, "ok": ok}


def cmd_type(args):
	"""送按鍵：text 用 KEYEVENTF_UNICODE；keys 用虛擬鍵名稱（NVDA KeyboardInputGesture.fromName）。"""
	from keyboardHandler import KeyboardInputGesture
	import brailleInput as bi
	for k in args.get("keys", []):
		KeyboardInputGesture.fromName(k).send()
	if args.get("text"):
		bi.handler.sendChars(args["text"])
	return {"ok": True}


def cmd_text(args):
	"""讀前景焦點物件的全部文字。"""
	import api
	import textInfos
	obj = api.getFocusObject()
	try:
		text = obj.makeTextInfo(textInfos.POSITION_ALL).text
	except Exception as e:
		text = "error: %r" % e
	return {"name": obj.name, "role": str(obj.role), "windowClass": obj.windowClassName, "text": text}


def cmd_plugin(args):
	"""讀取或呼叫其他 globalPlugin：{"module": "sixdIME", "get": ["_mode"], "call": "name", "args": [...]}"""
	import globalPluginHandler as gph
	plugin = next((p for p in gph.runningPlugins if type(p).__module__.endswith(args.get("module", "sixdIME"))), None)
	if plugin is None:
		return {"error": "plugin not found"}
	out = {}
	if args.get("call"):
		out["return"] = getattr(plugin, args["call"])(*args.get("args", []))
	for name in args.get("get", []):
		out[name] = getattr(plugin, name)
	return out


_sentSeqs = []


def cmd_sdlog(args):
	"""記錄 6d-IME 送給 IME 的大千鍵（含調號）：{"start": true} 開始並清空；{} 取回並清空。"""
	import globalPluginHandler as gph
	plugin = next(p for p in gph.runningPlugins if type(p).__module__.endswith("sixdIME"))
	if args.get("start"):
		if not getattr(plugin, "_harnessOrigSend", None):
			plugin._harnessOrigSend = plugin._send_daqian_seq

			def wrapped(seq):
				_sentSeqs.append(seq)
				return plugin._harnessOrigSend(seq)
			plugin._send_daqian_seq = wrapped
		_sentSeqs.clear()
		return {"ok": True}
	out = list(_sentSeqs)
	_sentSeqs.clear()
	return out


def cmd_keys(args):
	"""依序呼叫 6d-IME 的鍵盤 hook：[["down"|"up", "f"|"space"|...], ...]（模擬實體按鍵，不經過 SendInput）"""
	import globalPluginHandler as gph
	plugin = next(p for p in gph.runningPlugins if type(p).__module__.endswith("sixdIME"))
	scan = {"f": 0x21, "d": 0x20, "s": 0x1F, "j": 0x24, "k": 0x25, "l": 0x26, "a": 0x1E, ";": 0x27, "space": 0x39}
	vk = {"f": 0x46, "d": 0x44, "s": 0x53, "j": 0x4A, "k": 0x4B, "l": 0x4C, "a": 0x41, ";": 0xBA, "space": 0x20}
	results = []
	for action, key in args["seq"]:
		fn = plugin._on_key_down if action == "down" else plugin._on_key_up
		results.append(fn(vk[key], scan[key], False, False))
	return results


def cmd_reload(args):
	import globalPluginHandler as gph
	wx.CallLater(300, gph.reloadGlobalPlugins)
	return {"ok": True}


def cmd_convmode(args):
	"""前景焦點視窗輸入法的 conversion mode：{} 讀取；{"set": n} 設定（1=中文，0=英數）。"""
	import ctypes as C
	class GTI(C.Structure):
		_fields_ = [("cbSize", C.c_ulong), ("flags", C.c_ulong)] + [(n, C.c_void_p) for n in
			("a", "focus", "c", "d", "e", "f")] + [("rc", C.c_long * 4)]
	g = GTI(cbSize=C.sizeof(GTI))
	hwnd = user32.GetForegroundWindow()
	if user32.GetGUIThreadInfo(user32.GetWindowThreadProcessId(hwnd, None), C.byref(g)) and g.focus:
		hwnd = g.focus
	C.windll.imm32.ImmGetDefaultIMEWnd.restype = C.c_void_p
	ime = C.windll.imm32.ImmGetDefaultIMEWnd(C.c_void_p(hwnd))
	send = user32.SendMessageW
	send.argtypes = [C.c_void_p, C.c_uint, C.c_size_t, C.c_ssize_t]
	send.restype = C.c_ssize_t
	if "set" in args:
		send(ime, 0x283, 2, int(args["set"]))
	return {"imeWnd": ime, "mode": send(ime, 0x283, 1, 0)}


def cmd_bind(args):
	"""新增使用者輸入手勢並存檔（等同輸入手勢對話框）：
	{"gesture": "bk:space+dot8", "module": "globalCommands", "cls": "GlobalCommands", "script": "kb:control+shift+enter"}"""
	m = inputCore.manager.userGestureMap
	m.add(args["gesture"], args.get("module", "globalCommands"), args.get("cls", "GlobalCommands"), args["script"])
	m.save()
	return {"ok": True}


def cmd_unbind(args):
	"""移除使用者輸入手勢並存檔（參數同 bind）。"""
	m = inputCore.manager.userGestureMap
	m.remove(args["gesture"], args.get("module", "globalCommands"), args.get("cls", "GlobalCommands"), args["script"])
	m.save()
	return {"ok": True}


# 前景保護：設定後，前景視窗類別不符就拒絕送按鍵的指令（避免打進別的程式）
_guard = {"cls": None}
_KEY_CMDS = ("dots", "keys", "type")


def cmd_guard(args):
	"""{"cls": "Notepad"} 設定；{"cls": null} 取消。"""
	_guard["cls"] = args.get("cls")
	return {"cls": _guard["cls"]}


def _guard_blocked(cmd):
	if cmd not in _KEY_CMDS or not _guard["cls"]:
		return None
	buf = ctypes.create_unicode_buffer(256)
	hwnd = user32.GetForegroundWindow()
	user32.GetClassNameW(hwnd, buf, 256)
	if buf.value != _guard["cls"]:
		user32.GetWindowTextW(hwnd, buf, 256)
		return "guard: 前景不是 %s（%s），沒有送出" % (_guard["cls"], buf.value)
	return None


def cmd_braille(args):
	"""目前點顯器的內容與輸出表；{"text": "母"} 則用目前的輸出表轉譯該文字。"""
	def cells(c):
		return "".join(chr(0x2800 + x) for x in c)
	table = braille.handler.table
	out = {"table": getattr(table, "fileName", str(table))}
	if args.get("text"):
		import louisHelper
		import louis
		# 可選 cursor（游標位置，模擬 NVDA 在編輯區的轉譯：會加 compbrlAtCursor）、table（指定點字表）
		name = args.get("table", table.fileName)
		cursor = args.get("cursor")
		mode = louis.dotsIO | (louis.compbrlAtCursor if cursor is not None else 0)
		res = louisHelper.translate([name, "braille-patterns.cti"], args["text"], cursorPos=cursor, mode=mode)
		out["translated"] = cells(res[0])
		out["cursorCell"] = res[3]
	buf = braille.handler.mainBuffer
	out["rawText"] = getattr(buf, "windowRawText", None) or ""
	out["cells"] = cells(getattr(buf, "windowBrailleCells", []) or [])
	return out


COMMANDS = {
	"braille": cmd_braille,
	"state": cmd_state, "profiles": cmd_profiles, "profile_names": cmd_profile_names,
	"activate": cmd_activate, "dots": cmd_dots, "spoken": cmd_spoken,
	"focus": cmd_focus, "type": cmd_type, "text": cmd_text, "reload": cmd_reload,
	"plugin": cmd_plugin, "keys": cmd_keys, "sdlog": cmd_sdlog, "trace": cmd_trace, "guard": cmd_guard, "convmode": cmd_convmode, "bind": cmd_bind, "unbind": cmd_unbind, "ids": cmd_ids,
}


IPC_DIR = os.path.join(os.environ["TEMP"], "sixdDevHarness")
VERSION = "0.10"


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""檔案式 IPC：client 寫 req.json，這裡用 wx.Timer 在主執行緒輪詢、執行、寫 resp.json。"""

	def __init__(self):
		super().__init__()
		os.makedirs(IPC_DIR, exist_ok=True)
		self._timer = wx.Timer()
		self._timer.Bind(wx.EVT_TIMER, self._poll)
		self._timer.Start(200)
		try:
			import speech.extensions
			speech.extensions.pre_speech.register(_on_pre_speech)
		except Exception:
			log.warning("devHarness: pre_speech 無法註冊", exc_info=True)
		log.info("sixdDevHarness %s polling %s" % (VERSION, IPC_DIR))

	def _poll(self, evt):
		req_path = os.path.join(IPC_DIR, "req.json")
		if not os.path.exists(req_path):
			return
		try:
			with open(req_path, encoding="utf-8") as f:
				req = json.load(f)
			os.remove(req_path)
		except Exception:
			return  # client 可能還在寫，下一輪再讀
		try:
			blocked = _guard_blocked(req["cmd"])
			res = {"error": blocked} if blocked else {"result": COMMANDS[req["cmd"]](req.get("args", {}))}
		except Exception as e:
			log.error("devHarness", exc_info=True)
			res = {"error": repr(e)}
		res["id"] = req.get("id")
		tmp = os.path.join(IPC_DIR, "resp.tmp")
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump(res, f, ensure_ascii=False, default=str)
		os.replace(tmp, os.path.join(IPC_DIR, "resp.json"))

	def terminate(self):
		self._timer.Stop()
		try:
			import speech.extensions
			speech.extensions.pre_speech.unregister(_on_pre_speech)
		except Exception:
			pass
		super().terminate()
