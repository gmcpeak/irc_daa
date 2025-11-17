# frontend/app.py
import os
import base64
import dotenv
import requests
import streamlit as st

# Load .env baked into the image (not Azure env feature)
dotenv.load_dotenv(".env")

# For local docker-compose we’ll set this to "http://worker:8000".
# For Azure, you can either hard-code the worker URL here, or keep
# it in .env and rebuild the image when the worker URL changes.
WORKER_URL = os.getenv("WORKER_URL", "http://localhost:8000")

st.set_page_config(page_title="GW+IRC DAA", layout="wide")

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
st.markdown(
    "Use your phone or laptop camera to take a picture **or** upload one, "
    "then click **Process**. The heavy lifting now runs in a separate worker."
)

left, right = st.columns(2)
with left:
    cam = st.camera_input("Take a photo", key="camera")
with right:
    upl = st.file_uploader(
        "...or upload a photo", type=["png", "jpg", "jpeg"], key="uploader"
    )

uploaded = cam or upl

st.markdown("### Output")
out_area = st.empty()
status_area = st.empty()

if st.button("Process", type="primary", disabled=(uploaded is None)):
    if uploaded is None:
        st.warning("Please take or upload a photo first.")
    else:
        file_bytes = uploaded.getvalue()
        filename = uploaded.name or "image.png"
        mime_type = uploaded.type or "image/png"

        # Short spinner: only covers the HTTP submit, not full processing.
        with st.spinner("Submitting image to worker..."):
            try:
                resp = requests.post(
                    f"{WORKER_URL}/process",
                    files={"file": (filename, file_bytes, mime_type)},
                    timeout=30,  # shorter, since this is just an ACK
                )
            except Exception as e:
                status_area.error(f"Failed to contact worker: {e}")
                resp = None

        if resp is None:
            # Error already shown
            pass

        elif resp.status_code != 200:
            try:
                body = resp.json()
                detail = body.get("detail", str(body))
            except Exception:
                detail = resp.text

            status_area.error(
                f"Worker error (status {resp.status_code}): {detail}"
            )

        else:
            # SUCCESS ACK: worker has queued the job
            data = resp.json()

            expected_filename = data.get("expected_filename")
            status_msg = data.get("status_message")

            # We don't have immediate output/docx anymore; it's async.
            st.session_state.last_output = None
            st.session_state.docx_bytes = None
            st.session_state.docx_filename = expected_filename

            lines = []
            # Add a check mark at the start
            lines.append("✅ Image successfully submitted to worker.")

            if expected_filename:
                lines.append(f"Expected filename: **{expected_filename}**")

            if status_msg:
                lines.append(status_msg)
            else:
                lines.append(
                    "The worker has queued your request. "
                    "It is now safe to close this tab. "
                    "Your document will be uploaded to Box under the filename shown."
                )

            status_area.success("  \n".join(lines))

            # Clear output area, since we won't show model text anymore
            out_area.markdown(
                "_Processing is happening in the background on the worker. "
                "You can retrieve the document from Box when it is ready._"
            )

# # Download button (if worker finished at least once)
# if (
#     st.session_state.docx_bytes is not None
#     and st.session_state.docx_filename is not None
# ):
#     st.download_button(
#         label="Download as Word (.docx)",
#         data=st.session_state.docx_bytes,
#         file_name=st.session_state.docx_filename,
#         mime=(
#             "application/vnd.openxmlformats-officedocument."
#             "wordprocessingml.document"
#         ),
#         key="download_docx",
#     )
