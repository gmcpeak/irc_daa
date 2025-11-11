# app.py (JWT + .env)
import os
import io
from datetime import datetime
from typing import Optional, List

import re
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

import streamlit as st
from PIL import Image, ImageOps
from PIL.Image import Image as PILImage
from docx import Document
from openai import OpenAI

import logging
import dotenv

from box_sdk_gen import (
    BoxClient,
    BoxJWTAuth,
    FileWithInMemoryCacheTokenStorage,
    JWTConfig,
    BoxAPIError,
)
from box_sdk_gen.managers.uploads import (
    UploadFileAttributes,
    UploadFileAttributesParentField,
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("box_sdk_gen").setLevel(logging.CRITICAL)

# -------------------------------------------------
# load env (both general .env and .jwt.env if you like)
# -------------------------------------------------
dotenv.load_dotenv(".env")
dotenv.load_dotenv(".jwt.env")  # your JWT stuff lives here

# get OpenAI key from env
OPENAI_KEY = os.getenv("OPENAI_KEY")
print(OPENAI_KEY)

# -------------------------------------------------
# Streamlit page config
# -------------------------------------------------
st.set_page_config(page_title="GW+IRC DAA", layout="wide")

PROMPT_PATH = "./assets/system_prompt.txt"
DOC1_PATH   = "./assets/Goodwill-Donation-Value-Guide.txt"
DOC2_PATH   = "./assets/Salvation-Army-Donation-Value-Guide.txt"

client = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

# -------------------------------------------------
# JWT helpers (from your working jwt_login_test.py)
# -------------------------------------------------
def load_jwt_config_from_env() -> dict:
    jwt_config_path = os.getenv("JWT_CONFIG_PATH")
    jwt_user_id = os.getenv("JWT_USER_ID")
    cache_file = os.getenv("CACHE_FILE", ".jwt.tk")

    if not jwt_config_path:
        raise RuntimeError("JWT_CONFIG_PATH is not set (put it in .jwt.env or .env)")
    if not jwt_user_id:
        raise RuntimeError("JWT_USER_ID is not set (put it in .jwt.env or .env)")

    return {
        "jwt_config_path": jwt_config_path,
        "jwt_user_id": jwt_user_id,
        "cache_file": cache_file,
    }


def get_jwt_user_client(cfg: dict) -> BoxClient:
    jwt = JWTConfig.from_config_file(
        config_file_path=cfg["jwt_config_path"],
        token_storage=FileWithInMemoryCacheTokenStorage(".user" + cfg["cache_file"]),
    )
    auth = BoxJWTAuth(jwt).with_user_subject(cfg["jwt_user_id"])
    return BoxClient(auth)


def upload_docx_bytes(
    client: BoxClient,
    file_name: str,
    docx_bytes: bytes,
    folder_id: str = "0",
):
    stream = io.BytesIO(docx_bytes)
    attrs = UploadFileAttributes(
        name=file_name,
        parent=UploadFileAttributesParentField(id=folder_id),
    )
    try:
        res = client.uploads.upload_file(attrs, stream)
        uploaded = res.entries[0]
        return uploaded.id, uploaded.name
    except BoxAPIError as err:
        if err.response_info.body.get("code") == "item_name_in_use":
            conflicts = err.response_info.body["context_info"]["conflicts"]
            box_file_id = conflicts["id"]
            stream.seek(0)
            res = client.uploads.upload_file_version(box_file_id, stream, attrs)
            uploaded = res.entries[0]
            return uploaded.id, uploaded.name
        else:
            raise

# -------------------------------------------------
# misc helpers
# -------------------------------------------------
def _read_texts(paths: List[str]) -> str:
    parts: List[str] = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                parts.append(f.read())
        except Exception:
            pass
    return "\n".join(parts)


def _load_pil_from_uploaded(uploaded) -> Optional[PILImage]:
    if uploaded is None:
        return None
    try:
        img = Image.open(uploaded)
        return ImageOps.exif_transpose(img)
    except Exception:
        try:
            data = uploaded.getvalue()
            return ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        except Exception:
            return None


def _process(prompt_text: str, image: Optional[PILImage]) -> str:
    if client is None:
        return "OpenAI client not configured (missing OPENAI_KEY)."

    from base64 import b64encode

    content = []
    if prompt_text:
        content.append({"type": "input_text", "text": prompt_text})
    if isinstance(image, PILImage):
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        b64 = b64encode(buf.getvalue()).decode("utf-8")
        content.append({"type": "input_image", "image_url": f"data:image/png;base64,{b64}"})

    resp = client.responses.create(
        model="gpt-5",
        reasoning={"effort": "low"},
        text={"verbosity": "low"},
        tools=[{"type": "web_search"}],
        input=[{"role": "user", "content": content}] if content else prompt_text,
    )

    text = getattr(resp, "output_text", None)
    if not text:
        try:
            text = resp.output[0].content[0].text
        except Exception:
            text = str(resp)
    return text

def add_hyperlink(paragraph, text, url):
    """
    Add a hyperlink to a paragraph. Returns the hyperlink run.
    """
    # get the document part
    part = paragraph.part
    # create a relationship id
    r_id = part.relate_to(
        url,
        reltype="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )

    # build the w:hyperlink tag
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)

    # build a w:r
    new_run = OxmlElement("w:r")

    # build a w:rPr (run properties) so Word formats it like a link
    rPr = OxmlElement("w:rPr")

    # style: underline
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(u)

    # style: blue (Word's default link color is theme-based; this is a simple one)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0000FF")
    rPr.append(color)

    new_run.append(rPr)

    # add the text to the run
    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)

    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)

    return new_run

def _docx_bytes_from_text(
    text: str,
    image: Optional[PILImage],
    title: str = "Donation Audit",
) -> bytes:
    doc = Document()
    if title:
        doc.add_heading(title, level=0)

    # optional image
    if image is not None:
        img_buf = io.BytesIO()
        image.save(img_buf, format="PNG")
        img_buf.seek(0)
        section = doc.sections[0]
        max_width = section.page_width - section.left_margin - section.right_margin
        doc.add_picture(img_buf, width=max_width)
        doc.add_paragraph("")

    # now handle text with markdown links
    for line in (text or "").splitlines():
        # create paragraph for this line
        p = doc.add_paragraph()
        pos = 0
        for match in LINK_RE.finditer(line):
            start, end = match.span()
            link_text = match.group(1)
            link_url = match.group(2)

            # text before the link
            if start > pos:
                p.add_run(line[pos:start])

            # the link itself
            add_hyperlink(p, link_text, link_url)

            pos = end

        # any trailing text after the last link
        if pos < len(line):
            p.add_run(line[pos:])

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# -------------------------------------------------
# session state
# -------------------------------------------------
if "last_output" not in st.session_state:
    st.session_state.last_output = None
if "docx_bytes" not in st.session_state:
    st.session_state.docx_bytes = None
if "docx_filename" not in st.session_state:
    st.session_state.docx_filename = None

# -------------------------------------------------
# UI
# -------------------------------------------------
st.markdown("# GW+IRC Donation Audit Assistant")
st.markdown("Use your phone or laptop camera to take a picture **or** upload one, then click **Process**.")

left, right = st.columns(2)
with left:
    cam = st.camera_input("Take a photo", key="camera")
with right:
    upl = st.file_uploader("...or upload a photo", type=["png", "jpg", "jpeg"])

uploaded = cam or upl
image_pil = _load_pil_from_uploaded(uploaded)

# build JWT Box client
jwt_cfg = load_jwt_config_from_env()
box_client = get_jwt_user_client(jwt_cfg)
me = box_client.users.get_user_me()
st.caption(f"Connected to Box as **{me.name}** ({me.login})")

st.markdown("### Output")
out_area = st.empty()

if st.button("Process", type="primary", disabled=(image_pil is None)):
    system_prompt = _read_texts([PROMPT_PATH])
    guides_text   = _read_texts([DOC1_PATH, DOC2_PATH])
    prompt = (system_prompt or "") + "\n" + (guides_text or "")

    with st.spinner("Generating output..."):
        out_text = _process(prompt, image_pil)

    out_area.markdown(out_text.replace('$', r'\$') if out_text else "_No output._")

    st.session_state.last_output = out_text
    st.session_state.docx_bytes = _docx_bytes_from_text(out_text or "", image_pil)
    st.session_state.docx_filename = (
        f"GW-IRC-Donation-Value-Audit--{datetime.now().strftime('%Y%m%d-%H%M%S')}.docx"
    )

if st.session_state.docx_bytes and st.session_state.last_output:
    with st.spinner("Uploading to Box..."):
        file_id, file_name = upload_docx_bytes(
            box_client,
            st.session_state.docx_filename,
            st.session_state.docx_bytes,
            folder_id="0",
        )
    st.success(f"Uploaded to Box: {file_name} (id: {file_id})")

    st.download_button(
        label="Download as Word (.docx)",
        data=st.session_state.docx_bytes,
        file_name=st.session_state.docx_filename,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        key="download_docx",
    )
