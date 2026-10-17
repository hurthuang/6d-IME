# -*- coding: UTF-8 -*-
# 用 TSF（Text Services Framework）查詢、切換輸入法。
#
# 實測（Win11 新版記事本，2026-10-17）：
#   - ActivateProfile 不加旗標：回傳成功但沒有效果
#   - 加 TF_IPPMF_FORSESSION：從 NVDA 就能切換前景程式的輸入法
#     （倉頡→微軟注音、小麥注音→微軟注音、切回倉頡都成功，切過去是中文模式）
#   - 從 NVDA 執行緒 GetActiveProfile 讀到的是 NVDA 自己的輸入法，通常跟前景程式一樣，
#     但實測會脫鉤（NVDA 這邊是微軟注音、記事本是倉頡），判斷前景程式要改看
#     NVDA 收到的輸入法名稱，再用 description()／find_by_name() 對照
# TSF 是 STA COM，所有函式都要在 NVDA 主執行緒呼叫。

import ctypes
from ctypes import wintypes, POINTER, byref, Structure, c_ushort

import comtypes.client
from comtypes import GUID, IUnknown, COMMETHOD, HRESULT, BSTR
from logHandler import log

TF_PROFILETYPE_INPUTPROCESSOR = 1
TF_IPPMF_FORSESSION = 0x20000000
TF_IPP_FLAG_ENABLED = 0x2
GUID_TFCAT_TIP_KEYBOARD = GUID("{34745C63-B2F0-4784-8B67-5E12C8701A31}")
CLSID_TF_InputProcessorProfiles = GUID("{33C53A50-F456-4884-B049-85FD643ECFED}")
LANGID_ZH_TW = 0x0404

# 微軟注音
MS_BOPOMOFO = (GUID("{B115690A-EA02-48D5-A231-E3578D2FDF80}"),
               GUID("{B2F9C502-1742-11D4-9790-0080C882687E}"))


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
        COMMETHOD([], HRESULT, "DeactivateProfile"),   # 以下到 GetActiveProfile 前只佔 vtable 位置
        COMMETHOD([], HRESULT, "GetProfile"),
        COMMETHOD([], HRESULT, "EnumProfiles",
                  (["in"], c_ushort, "langid"),
                  (["out"], POINTER(POINTER(IEnumTfInputProcessorProfiles)), "ppEnum")),
        COMMETHOD([], HRESULT, "ReleaseInputProcessor"),
        COMMETHOD([], HRESULT, "RegisterProfile"),
        COMMETHOD([], HRESULT, "UnregisterProfile"),
        COMMETHOD([], HRESULT, "GetActiveProfile",
                  (["in"], POINTER(GUID), "catid"),
                  (["in"], POINTER(TF_INPUTPROCESSORPROFILE), "pProfile")),
    ]


class ITfInputProcessorProfiles(IUnknown):
    _iid_ = GUID("{1F02B6C5-7842-4EE6-8A0B-9A24183A95CA}")
    _methods_ = [COMMETHOD([], HRESULT, name) for name in (   # 只佔 vtable 位置
        "Register", "Unregister", "AddLanguageProfile", "RemoveLanguageProfile",
        "EnumInputProcessorInfo", "GetDefaultLanguageProfile", "SetDefaultLanguageProfile",
        "ActivateLanguageProfile", "GetActiveLanguageProfile")] + [
        COMMETHOD([], HRESULT, "GetLanguageProfileDescription",
                  (["in"], POINTER(GUID), "rclsid"),
                  (["in"], c_ushort, "langid"),
                  (["in"], POINTER(GUID), "guidProfile"),
                  (["out"], POINTER(BSTR), "pbstrProfile")),
    ]


def _mgr():
    return comtypes.client.CreateObject(CLSID_TF_InputProcessorProfiles,
                                        interface=ITfInputProcessorProfileMgr)


def description(langid, clsid, profile):
    """輸入法的顯示名稱（跟 NVDA 念的一樣，例如「微軟倉頡」）；失敗回傳 None。"""
    try:
        p = comtypes.client.CreateObject(CLSID_TF_InputProcessorProfiles,
                                         interface=ITfInputProcessorProfiles)
        return p.GetLanguageProfileDescription(byref(clsid), langid, byref(profile))
    except Exception:
        log.debug("6d-IME tsf: GetLanguageProfileDescription 失敗", exc_info=True)
        return None


def ms_bopomofo_name():
    return description(LANGID_ZH_TW, *MS_BOPOMOFO)


def find_by_name(name):
    """在已啟用的輸入法裡找顯示名稱是 name 的，回傳 get_active() 格式；找不到回傳 None。"""
    try:
        enum = _mgr().EnumProfiles(0)   # 0 = 所有語言
        while True:
            p = TF_INPUTPROCESSORPROFILE()
            n = wintypes.ULONG()
            enum.Next(1, byref(p), byref(n))
            if n.value == 0:
                return None
            if p.dwProfileType != TF_PROFILETYPE_INPUTPROCESSOR or not p.dwFlags & TF_IPP_FLAG_ENABLED:
                continue
            clsid, profile = GUID(str(p.clsid)), GUID(str(p.guidProfile))
            if description(p.langid, clsid, profile) == name:
                return (p.langid, clsid, profile, 0)
    except Exception:
        log.debug("6d-IME tsf: find_by_name 失敗", exc_info=True)
        return None


def get_active():
    """回傳目前鍵盤輸入法 (langid, clsid, profile)；失敗回傳 None。"""
    try:
        p = TF_INPUTPROCESSORPROFILE()
        _mgr().GetActiveProfile(byref(GUID_TFCAT_TIP_KEYBOARD), byref(p))
        return (p.langid, GUID(str(p.clsid)), GUID(str(p.guidProfile)), p.hkl or 0)
    except Exception:
        log.debug("6d-IME tsf: GetActiveProfile 失敗", exc_info=True)
        return None


def is_ms_bopomofo(active):
    return bool(active) and active[1] == MS_BOPOMOFO[0] and active[2] == MS_BOPOMOFO[1]


def ms_bopomofo_enabled():
    """微軟注音有沒有安裝並啟用（在 Windows 語言設定的輸入法清單裡）。"""
    try:
        enum = _mgr().EnumProfiles(LANGID_ZH_TW)
        while True:
            p = TF_INPUTPROCESSORPROFILE()
            n = wintypes.ULONG()
            enum.Next(1, byref(p), byref(n))
            if n.value == 0:
                return False
            if (GUID(str(p.clsid)) == MS_BOPOMOFO[0] and GUID(str(p.guidProfile)) == MS_BOPOMOFO[1]
                    and p.dwFlags & TF_IPP_FLAG_ENABLED):
                return True
    except Exception:
        log.debug("6d-IME tsf: EnumProfiles 失敗", exc_info=True)
        return False


def activate(active):
    """切換到 get_active() 回傳的那個輸入法；成功回傳 True。"""
    langid, clsid, profile, hkl = active
    try:
        if clsid == GUID("{00000000-0000-0000-0000-000000000000}"):
            # 一般鍵盤配置（不是 TSF 輸入法）
            _mgr().ActivateProfile(2, langid, byref(clsid), byref(profile), hkl, TF_IPPMF_FORSESSION)
        else:
            _mgr().ActivateProfile(TF_PROFILETYPE_INPUTPROCESSOR, langid,
                                   byref(clsid), byref(profile), 0, TF_IPPMF_FORSESSION)
        return True
    except Exception:
        log.debug("6d-IME tsf: ActivateProfile 失敗", exc_info=True)
        return False


def activate_ms_bopomofo():
    return activate((LANGID_ZH_TW, MS_BOPOMOFO[0], MS_BOPOMOFO[1], 0))
