"""Certificate-based digital signatures (PAdES / PKCS#7) with pyHanko: sign, verify, personal certificates."""
from __future__ import annotations

import datetime
import logging
import os
import shutil
import tempfile

import pymupdf

from .config import DATA_DIR

CERT_DIR = DATA_DIR / "certificates"
logging.getLogger("pyhanko").setLevel(logging.CRITICAL)
logging.getLogger("pyhanko_certvalidator").setLevel(logging.CRITICAL)


class SignError(Exception):
    pass


# ------------------------------------------------------------------ certificates
def list_certificates() -> list[str]:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(str(p) for p in CERT_DIR.iterdir() if p.suffix.lower() in (".p12", ".pfx"))


def import_certificate(path: str) -> str:
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    dst = CERT_DIR / os.path.basename(path)
    if os.path.abspath(path) != str(dst):
        shutil.copyfile(path, dst)
    return str(dst)


def create_self_signed(name: str, email: str, org: str, password: str, years: int = 5) -> str:
    """Personal certificate (valid signature, but not issued by a recognised authority)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if org:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org))
    if email:
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email))
    subject = x509.Name(attrs)
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=365 * years))
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
            .sign(key, hashes.SHA256()))
    data = pkcs12.serialize_key_and_certificates(name.encode("utf-8"), key, cert, None,
                                                 serialization.BestAvailableEncryption(password.encode("utf-8")))
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in " -_").strip() or "certificate"
    path = CERT_DIR / f"{safe}.p12"
    i = 1
    while path.exists():
        path = CERT_DIR / f"{safe} ({i}).p12"
        i += 1
    path.write_bytes(data)
    return str(path)


def certificate_summary(path: str, password: str) -> dict:
    from cryptography.hazmat.primitives.serialization import pkcs12

    with open(path, "rb") as fh:
        try:
            _key, cert, _extra = pkcs12.load_key_and_certificates(fh.read(), password.encode("utf-8"))
        except ValueError as e:
            raise SignError("bad_password") from e
    if cert is None:
        raise SignError("no_certificate")
    cn = cert.subject.rfc4514_string()
    for attr in cert.subject:
        if attr.oid.dotted_string == "2.5.4.3":
            cn = attr.value
    return {"name": cn, "issuer": cert.issuer.rfc4514_string(),
            "self_signed": cert.issuer == cert.subject,
            "expires": cert.not_valid_after_utc.strftime("%Y-%m-%d")}


# ---------------------------------------------------------------------- sign
def _pdf_box(page: pymupdf.Page, rect_unrot: pymupdf.Rect) -> tuple[float, float, float, float]:
    r = (rect_unrot * ~page.transformation_matrix).normalize()
    return (r.x0, r.y0, r.x1, r.y1)


def signed_output_path(src: str) -> str:
    from .convert import unique_path

    return unique_path(os.path.splitext(src)[0] + " (signed).pdf")


def sign_pdf(src: str, dst: str, cert_path: str, password: str, *, page: int | None = None,
             rect: pymupdf.Rect | None = None, image: bytes | None = None, reason: str = "",
             location: str = "", stamp_label: str = "Digitally signed by %(signer)s\n%(ts)s",
             doc_password: str | None = None) -> str:
    """Sign `src` into `dst`. `rect` (unrotated page coordinates) makes the signature visible; an optional
    handwritten `image` is drawn in its left part."""
    from pyhanko import stamp
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.pdf_utils.text import TextBoxStyle
    from pyhanko.sign import fields, signers

    if cert_path.startswith("win:"):
        from .winstore import make_signer

        signer = make_signer(cert_path[4:])
    else:
        signer = signers.SimpleSigner.load_pkcs12(cert_path, passphrase=password.encode("utf-8"))
        if signer is None:
            raise SignError("bad_password")
    work = tempfile.mktemp(prefix="msr_sign_", suffix=".pdf", dir=os.path.dirname(dst) or None)
    shutil.copyfile(src, work)
    try:
        box = None
        if rect is not None and page is not None:
            doc = pymupdf.open(work)
            if doc.needs_pass:
                doc.authenticate(doc_password or "")
            pg = doc.load_page(page)
            sig_rect = pymupdf.Rect(rect)
            if image:
                split = rect.x0 + rect.width * 0.55
                pg.insert_image(pymupdf.Rect(rect.x0, rect.y0, split, rect.y1), stream=image, keep_proportion=True)
                sig_rect = pymupdf.Rect(split, rect.y0, rect.x1, rect.y1)
                doc.saveIncr()  # incremental: keeps earlier signatures intact
            box = _pdf_box(pg, sig_rect)
            existing = [w.field_name for w in pg.widgets()] + [
                w.field_name for i in range(doc.page_count) for w in doc.load_page(i).widgets()]
            doc.close()
        else:
            doc = pymupdf.open(work)
            existing = [w.field_name for i in range(doc.page_count) for w in doc.load_page(i).widgets()]
            doc.close()
        n = 1
        while f"Signature{n}" in existing:
            n += 1
        field = f"Signature{n}"
        with open(work, "rb") as inf:
            w = IncrementalPdfFileWriter(inf, strict=False)
            if doc_password:
                w.encrypt(doc_password.encode("utf-8"))
            spec = fields.SigFieldSpec(field, on_page=page or 0, box=box) if box else fields.SigFieldSpec(field)
            fields.append_signature_field(w, spec)
            meta = signers.PdfSignatureMetadata(field_name=field, reason=reason or None,
                                                location=location or None)
            style = stamp.TextStampStyle(stamp_text=stamp_label, border_width=1,
                                         text_box_style=TextBoxStyle(font_size=8))
            pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=style)
            part = dst + ".part"
            with open(part, "wb") as outf:
                pdf_signer.sign_pdf(w, output=outf)
        os.replace(part, dst)
        return dst
    finally:
        store = getattr(signer, "_msr_store", None)
        if store is not None:
            store.close()
        for p in (work, dst + ".part"):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


# -------------------------------------------------------------------- verify
_TRUST = None


def _windows_roots():
    global _TRUST
    if _TRUST is None:
        import ssl

        from asn1crypto import x509 as asn1x509
        roots = []
        for store in ("ROOT", "CA"):
            try:
                for der, enc, _trust in ssl.enum_certificates(store):
                    if enc == "x509_asn":
                        try:
                            roots.append(asn1x509.Certificate.load(der))
                        except Exception:  # noqa: BLE001
                            pass
            except (AttributeError, OSError):
                pass
        _TRUST = roots
    return _TRUST


def verify(path: str, doc_password: str | None = None) -> list[dict]:
    """Status of every signature: signer, date, intact (not altered), trusted (chain to a Windows root),
    and whether the document was changed after signing."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    out = []
    with open(path, "rb") as fh:
        reader = PdfFileReader(fh, strict=False)
        if doc_password and reader.encrypted:
            reader.decrypt(doc_password.encode("utf-8"))
        vc = ValidationContext(trust_roots=_windows_roots(), allow_fetching=False)
        for sig in reader.embedded_signatures:
            info = {"field": sig.field_name, "signer": "?", "time": None, "intact": False, "valid": False,
                    "trusted": False, "whole_file": False, "error": ""}
            try:
                st = validate_pdf_signature(sig, vc)
                cert = st.signing_cert
                info["signer"] = cert.subject.native.get("common_name") or cert.subject.human_friendly
                info["intact"], info["valid"], info["trusted"] = st.intact, st.valid, st.trusted
                info["whole_file"] = str(st.coverage).endswith("ENTIRE_FILE")
                ts = sig.self_reported_timestamp
                info["time"] = ts.astimezone().strftime("%Y-%m-%d %H:%M") if ts else None
            except Exception as e:  # noqa: BLE001
                info["error"] = str(e)
            out.append(info)
    return out


def has_signatures(doc: pymupdf.Document) -> bool:
    try:
        return doc.is_pdf and doc.get_sigflags() in (1, 3)
    except Exception:  # noqa: BLE001
        return False
