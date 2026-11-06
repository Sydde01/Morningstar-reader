"""Sign with certificates from the Windows certificate store (personal store, smart cards, USB tokens).

The private key never leaves Windows: the hash is signed through CNG (NCryptSignHash) or legacy
CryptoAPI, so smart cards and tokens with a Windows driver ("minidriver") work, and Windows itself
asks for the PIN.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import POINTER, Structure, byref, c_ubyte, c_void_p, c_wchar_p, sizeof
from ctypes import wintypes as wt

IS_WINDOWS = os.name == "nt"

CERT_KEY_PROV_INFO_PROP_ID = 2
CERT_NCRYPT_KEY_HANDLE_PROP_ID = 78
# Extended key usages that make sense for signing documents
SIGNING_EKUS = {"email_protection", "client_auth", "any_extended_key_usage", "1.3.6.1.4.1.311.10.3.12",
                "1.2.840.113583.1.1.5", "1.3.6.1.5.5.7.3.36"}
CRYPT_ACQUIRE_CACHE_FLAG = 0x1
CRYPT_ACQUIRE_PREFER_NCRYPT_KEY_FLAG = 0x20000
CERT_NCRYPT_KEY_SPEC = 0xFFFFFFFF
BCRYPT_PAD_PKCS1 = 0x2
CALG = {"sha1": 0x8004, "sha256": 0x800C, "sha384": 0x800D, "sha512": 0x800E}
HP_HASHVAL = 0x2


class CERT_CONTEXT(Structure):
    _fields_ = [("dwCertEncodingType", wt.DWORD), ("pbCertEncoded", POINTER(c_ubyte)),
                ("cbCertEncoded", wt.DWORD), ("pCertInfo", c_void_p), ("hCertStore", c_void_p)]


PCCERT_CONTEXT = POINTER(CERT_CONTEXT)


class CERT_TRUST_STATUS(Structure):
    _fields_ = [("dwErrorStatus", wt.DWORD), ("dwInfoStatus", wt.DWORD)]


class CERT_CHAIN_ELEMENT(Structure):
    _fields_ = [("cbSize", wt.DWORD), ("pCertContext", PCCERT_CONTEXT), ("TrustStatus", CERT_TRUST_STATUS),
                ("pRevocationInfo", c_void_p), ("pIssuanceUsage", c_void_p), ("pApplicationUsage", c_void_p),
                ("pwszExtendedErrorInfo", c_wchar_p)]


class CERT_SIMPLE_CHAIN(Structure):
    _fields_ = [("cbSize", wt.DWORD), ("TrustStatus", CERT_TRUST_STATUS), ("cElement", wt.DWORD),
                ("rgpElement", POINTER(POINTER(CERT_CHAIN_ELEMENT))), ("pTrustListInfo", c_void_p),
                ("fHasRevocationFreshnessTime", wt.BOOL), ("dwRevocationFreshnessTime", wt.DWORD)]


class CERT_CHAIN_CONTEXT(Structure):
    _fields_ = [("cbSize", wt.DWORD), ("TrustStatus", CERT_TRUST_STATUS), ("cChain", wt.DWORD),
                ("rgpChain", POINTER(POINTER(CERT_SIMPLE_CHAIN)))]


class CERT_USAGE_MATCH(Structure):
    _fields_ = [("dwType", wt.DWORD), ("cUsageIdentifier", wt.DWORD), ("rgpszUsageIdentifier", c_void_p)]


class CERT_CHAIN_PARA(Structure):
    _fields_ = [("cbSize", wt.DWORD), ("RequestedUsage", CERT_USAGE_MATCH)]


class BCRYPT_PKCS1_PADDING_INFO(Structure):
    _fields_ = [("pszAlgId", c_wchar_p)]


_api = None


def _lib():
    global _api
    if _api is None:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        ncrypt = ctypes.WinDLL("ncrypt", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        crypt32.CertOpenSystemStoreW.argtypes = [c_void_p, c_wchar_p]
        crypt32.CertOpenSystemStoreW.restype = c_void_p
        crypt32.CertEnumCertificatesInStore.argtypes = [c_void_p, PCCERT_CONTEXT]
        crypt32.CertEnumCertificatesInStore.restype = PCCERT_CONTEXT
        crypt32.CertGetCertificateContextProperty.argtypes = [PCCERT_CONTEXT, wt.DWORD, c_void_p,
                                                              POINTER(wt.DWORD)]
        crypt32.CertGetCertificateContextProperty.restype = wt.BOOL
        crypt32.CertDuplicateCertificateContext.argtypes = [PCCERT_CONTEXT]
        crypt32.CertDuplicateCertificateContext.restype = PCCERT_CONTEXT
        crypt32.CertFreeCertificateContext.argtypes = [PCCERT_CONTEXT]
        crypt32.CertCloseStore.argtypes = [c_void_p, wt.DWORD]
        crypt32.CryptAcquireCertificatePrivateKey.argtypes = [PCCERT_CONTEXT, wt.DWORD, c_void_p,
                                                              POINTER(ctypes.c_size_t), POINTER(wt.DWORD),
                                                              POINTER(wt.BOOL)]
        crypt32.CryptAcquireCertificatePrivateKey.restype = wt.BOOL
        crypt32.CertGetCertificateChain.argtypes = [c_void_p, PCCERT_CONTEXT, c_void_p, c_void_p,
                                                    POINTER(CERT_CHAIN_PARA), wt.DWORD, c_void_p,
                                                    POINTER(POINTER(CERT_CHAIN_CONTEXT))]
        crypt32.CertGetCertificateChain.restype = wt.BOOL
        crypt32.CertFreeCertificateChain.argtypes = [POINTER(CERT_CHAIN_CONTEXT)]
        ncrypt.NCryptSignHash.argtypes = [ctypes.c_size_t, c_void_p, POINTER(c_ubyte), wt.DWORD,
                                          POINTER(c_ubyte), wt.DWORD, POINTER(wt.DWORD), wt.DWORD]
        ncrypt.NCryptSignHash.restype = ctypes.c_long
        ncrypt.NCryptFreeObject.argtypes = [ctypes.c_size_t]
        advapi.CryptCreateHash.argtypes = [ctypes.c_size_t, wt.DWORD, ctypes.c_size_t, wt.DWORD,
                                           POINTER(ctypes.c_size_t)]
        advapi.CryptSetHashParam.argtypes = [ctypes.c_size_t, wt.DWORD, POINTER(c_ubyte), wt.DWORD]
        advapi.CryptSignHashW.argtypes = [ctypes.c_size_t, wt.DWORD, c_wchar_p, wt.DWORD, POINTER(c_ubyte),
                                          POINTER(wt.DWORD)]
        advapi.CryptDestroyHash.argtypes = [ctypes.c_size_t]
        advapi.CryptReleaseContext.argtypes = [ctypes.c_size_t, wt.DWORD]
        _api = (crypt32, ncrypt, advapi)
    return _api


def _der(ctx) -> bytes:
    c = ctx.contents
    return ctypes.string_at(c.pbCertEncoded, c.cbCertEncoded)


def _has_private_key(ctx) -> bool:
    crypt32 = _lib()[0]
    for prop in (CERT_KEY_PROV_INFO_PROP_ID, CERT_NCRYPT_KEY_HANDLE_PROP_ID):
        size = wt.DWORD(0)
        if crypt32.CertGetCertificateContextProperty(ctx, prop, None, byref(size)):
            return True
    return False


def list_signing_certificates() -> list[dict]:
    """Personal certificates that have a private key (software, smart card or token)."""
    if not IS_WINDOWS:
        return []
    from asn1crypto import x509

    crypt32 = _lib()[0]
    store = crypt32.CertOpenSystemStoreW(None, "MY")
    if not store:
        return []
    out = []
    ctx = None
    try:
        while True:
            ctx = crypt32.CertEnumCertificatesInStore(store, ctx)
            if not ctx:
                break
            if not _has_private_key(ctx):
                continue
            der = _der(ctx)
            try:
                cert = x509.Certificate.load(der)
                ku = cert.key_usage_value
                if ku is not None and not ({"digital_signature", "non_repudiation"} & set(ku.native)):
                    continue
                eku = cert.extended_key_usage_value
                if eku is not None:
                    names = {str(u.native) for u in eku} | {u.dotted for u in eku}
                    if not (SIGNING_EKUS & names):
                        continue
                if cert.self_signed != "no":
                    continue  # app / test certificates; personal certificates are used through .p12 files
                subj = cert.subject.native
                name = subj.get("common_name") or cert.subject.human_friendly
                out.append({"thumbprint": hashlib.sha1(der).hexdigest(), "name": name,
                            "issuer": cert.issuer.native.get("common_name") or cert.issuer.human_friendly,
                            "expires": cert["tbs_certificate"]["validity"]["not_after"].native.strftime("%Y-%m-%d"),
                            "self_signed": cert.self_signed != "no"})
            except Exception:  # noqa: BLE001
                continue
    finally:
        crypt32.CertCloseStore(store, 0)
    return out


class _StoreCert:
    """An open store + duplicated certificate context, found by thumbprint."""

    def __init__(self, thumbprint: str, store=None):
        crypt32 = _lib()[0]
        self._own_store = store is None
        self.store = crypt32.CertOpenSystemStoreW(None, "MY") if store is None else store
        self.ctx = None
        ctx = None
        while True:
            ctx = crypt32.CertEnumCertificatesInStore(self.store, ctx)
            if not ctx:
                break
            if hashlib.sha1(_der(ctx)).hexdigest() == thumbprint:
                self.ctx = crypt32.CertDuplicateCertificateContext(ctx)
                crypt32.CertFreeCertificateContext(ctx)
                break
        if not self.ctx:
            self.close()
            raise LookupError("certificate_not_found")

    def der(self) -> bytes:
        return _der(self.ctx)

    def chain(self) -> list[bytes]:
        crypt32 = _lib()[0]
        para = CERT_CHAIN_PARA()
        para.cbSize = sizeof(CERT_CHAIN_PARA)
        pchain = POINTER(CERT_CHAIN_CONTEXT)()
        out = []
        if crypt32.CertGetCertificateChain(None, self.ctx, None, self.ctx.contents.hCertStore, byref(para), 0,
                                           None, byref(pchain)):
            try:
                cc = pchain.contents
                if cc.cChain:
                    simple = cc.rgpChain[0].contents
                    for i in range(simple.cElement):
                        out.append(_der(simple.rgpElement[i].contents.pCertContext))
            finally:
                crypt32.CertFreeCertificateChain(pchain)
        return out or [self.der()]

    def sign_hash(self, digest: bytes, algo: str, key_type: str) -> bytes:
        crypt32, ncrypt, advapi = _lib()
        handle = ctypes.c_size_t(0)
        spec = wt.DWORD(0)
        must_free = wt.BOOL(False)
        if not crypt32.CryptAcquireCertificatePrivateKey(self.ctx, CRYPT_ACQUIRE_PREFER_NCRYPT_KEY_FLAG, None,
                                                         byref(handle), byref(spec), byref(must_free)):
            err = ctypes.get_last_error()
            # keys attached as a CNG handle (e.g. imported without persistence)
            size = wt.DWORD(sizeof(ctypes.c_size_t))
            if not crypt32.CertGetCertificateContextProperty(self.ctx, CERT_NCRYPT_KEY_HANDLE_PROP_ID,
                                                             byref(handle), byref(size)):
                raise OSError(err, "No usable private key for this certificate (CryptAcquireCertificatePrivateKey)")
            spec.value = CERT_NCRYPT_KEY_SPEC
            must_free.value = False
        buf = (c_ubyte * len(digest)).from_buffer_copy(digest)
        try:
            if spec.value == CERT_NCRYPT_KEY_SPEC:
                pad = BCRYPT_PKCS1_PADDING_INFO(algo.upper())
                pinfo, flags = (byref(pad), BCRYPT_PAD_PKCS1) if key_type == "rsa" else (None, 0)
                size = wt.DWORD(0)
                st = ncrypt.NCryptSignHash(handle.value, pinfo, buf, len(digest), None, 0, byref(size), flags)
                if st != 0:
                    raise OSError(st & 0xFFFFFFFF, f"NCryptSignHash failed (0x{st & 0xFFFFFFFF:08X})")
                sig = (c_ubyte * size.value)()
                st = ncrypt.NCryptSignHash(handle.value, pinfo, buf, len(digest), sig, size, byref(size), flags)
                if st != 0:
                    raise OSError(st & 0xFFFFFFFF, f"NCryptSignHash failed (0x{st & 0xFFFFFFFF:08X})")
                return bytes(sig[:size.value])
            # legacy CryptoAPI key (RSA only)
            h = ctypes.c_size_t(0)
            if not advapi.CryptCreateHash(handle.value, CALG[algo], 0, 0, byref(h)):
                raise OSError(ctypes.get_last_error(), "CryptCreateHash failed")
            try:
                if not advapi.CryptSetHashParam(h.value, HP_HASHVAL, buf, 0):
                    raise OSError(ctypes.get_last_error(), "CryptSetHashParam failed")
                size = wt.DWORD(0)
                advapi.CryptSignHashW(h.value, spec.value, None, 0, None, byref(size))
                sig = (c_ubyte * size.value)()
                if not advapi.CryptSignHashW(h.value, spec.value, None, 0, sig, byref(size)):
                    raise OSError(ctypes.get_last_error(), "CryptSignHash failed")
                return bytes(sig[:size.value])[::-1]  # CryptoAPI is little-endian
            finally:
                advapi.CryptDestroyHash(h.value)
        finally:
            if must_free.value:
                if spec.value == CERT_NCRYPT_KEY_SPEC:
                    ncrypt.NCryptFreeObject(handle.value)
                else:
                    advapi.CryptReleaseContext(handle.value, 0)

    def close(self) -> None:
        crypt32 = _lib()[0]
        if self.ctx:
            crypt32.CertFreeCertificateContext(self.ctx)
            self.ctx = None
        if self.store and self._own_store:
            crypt32.CertCloseStore(self.store, 0)
        self.store = None


def make_signer(thumbprint: str, store=None):
    """A pyHanko signer that delegates the private-key operation to Windows."""
    from asn1crypto import algos, x509
    from pyhanko.sign import signers
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    sc = _StoreCert(thumbprint, store)
    cert = x509.Certificate.load(sc.der())
    chain = [x509.Certificate.load(d) for d in sc.chain()]
    key_type = cert.public_key.algorithm  # "rsa" | "ec"
    if key_type not in ("rsa", "ec"):
        sc.close()
        raise ValueError(f"unsupported key type: {key_type}")
    key_bytes = (cert.public_key.bit_size + 7) // 8

    class WindowsStoreSigner(signers.Signer):
        async def async_sign_raw(self, data: bytes, digest_algorithm: str, dry_run=False) -> bytes:
            if dry_run:
                return bytes(key_bytes if key_type == "rsa" else 2 * key_bytes + 8)
            algo = digest_algorithm.lower().replace("-", "")
            digest = hashlib.new(algo, data).digest()
            raw = sc.sign_hash(digest, algo, key_type)
            if key_type == "ec":
                raw = algos.DSASignature.from_p1363(raw).dump()
            return raw

    mech = algos.SignedDigestAlgorithm({"algorithm": "sha256_rsa" if key_type == "rsa" else "sha256_ecdsa"})
    signer = WindowsStoreSigner(signing_cert=cert, cert_registry=SimpleCertificateStore.from_certs(chain),
                                signature_mechanism=mech)
    signer._msr_store = sc  # keep the handles alive while signing
    return signer
