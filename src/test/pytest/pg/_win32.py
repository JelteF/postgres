# Copyright (c) 2025, PostgreSQL Global Development Group

import ctypes
import platform


def current_windows_user():
    """
    A port of pg_regress.c's current_windows_user() helper. Returns
    (accountname, domainname).

    XXX This is dead code now, but I'm keeping it as a motivating example of
    Win32 interaction, and someone may find it useful in the future when writing
    SSPI tests?
    """
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        raise RuntimeError(
            f"current_windows_user() is not supported on {platform.system()}"
        )

    def raise_winerror_when_false(result, func, arguments):
        """
        A ctypes errcheck handler that raises WinError (which will contain the
        result of GetLastError()) when the function's return value is false.
        """
        if not result:
            raise ctypes.WinError()

    #
    # Function Prototypes
    #

    from ctypes import wintypes

    # GetCurrentProcess
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []

    # OpenProcessToken
    TOKEN_READ = 0x00020008

    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.PHANDLE,
    ]
    advapi32.OpenProcessToken.errcheck = raise_winerror_when_false

    # GetTokenInformation
    PSID = wintypes.LPVOID  # we don't need the internals
    TOKEN_INFORMATION_CLASS = wintypes.INT
    TokenUser = 1

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Sid", PSID),
            ("Attributes", wintypes.DWORD),
        ]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [
            ("User", SID_AND_ATTRIBUTES),
        ]

    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        TOKEN_INFORMATION_CLASS,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.PDWORD,
    ]
    advapi32.GetTokenInformation.errcheck = raise_winerror_when_false

    # LookupAccountSid
    SID_NAME_USE = wintypes.INT
    PSID_NAME_USE = ctypes.POINTER(SID_NAME_USE)

    advapi32.LookupAccountSidW.restype = wintypes.BOOL
    advapi32.LookupAccountSidW.argtypes = [
        wintypes.LPCWSTR,
        PSID,
        wintypes.LPWSTR,
        wintypes.LPDWORD,
        wintypes.LPWSTR,
        wintypes.LPDWORD,
        PSID_NAME_USE,
    ]
    advapi32.LookupAccountSidW.errcheck = raise_winerror_when_false

    #
    # Implementation (see pg_SSPI_recv_auth())
    #

    # Get the current process token...
    token = wintypes.HANDLE()
    proc = kernel32.GetCurrentProcess()
    advapi32.OpenProcessToken(proc, TOKEN_READ, token)

    # ...then read the TOKEN_USER struct for that token...
    info = TOKEN_USER()
    infolen = wintypes.DWORD()

    try:
        # (GetTokenInformation creates a buffer bigger than TOKEN_USER, so we
        # have to query the correct length first.)
        advapi32.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(infolen))
        assert False, "GetTokenInformation succeeded unexpectedly"

    except OSError as err:
        assert err.winerror == 122  # insufficient buffer

        ctypes.resize(info, infolen.value)
        advapi32.GetTokenInformation(
            token,
            TokenUser,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(infolen),
        )

    # ...then pull the account and domain names out of the user SID.
    MAXPGPATH = 1024

    account = ctypes.create_unicode_buffer(MAXPGPATH)
    domain = ctypes.create_unicode_buffer(MAXPGPATH)
    accountlen = wintypes.DWORD(ctypes.sizeof(account))
    domainlen = wintypes.DWORD(ctypes.sizeof(domain))
    use = SID_NAME_USE()

    advapi32.LookupAccountSidW(
        None,
        info.User.Sid,
        account,
        ctypes.byref(accountlen),
        domain,
        ctypes.byref(domainlen),
        ctypes.byref(use),
    )

    return (account.value, domain.value)
