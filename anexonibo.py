import os
import json
import requests
import streamlit as st

NIBO_BASE = "https://api.nibo.com.br/empresas/v1"

def nibo_headers():
    headers = {
        "X-API-Key": os.environ.get("NIBO_API_KEY", "").strip(),
        # o upload usa multipart, então não definir content-type fixo aqui
        "Accept": "application/json",
    }
    user_id = os.environ.get("NIBO_USER_ID", "").strip()
    if user_id:
        headers["X-User-Id"] = user_id
    return headers

def upload_file_to_nibo(file_name: str, file_bytes: bytes) -> dict:
    """
    Faz upload de UM arquivo para o Nibo e retorna o JSON de resposta.
    Doc: POST /files (multipart/form-data, campo 'file'). Retorna um FileId. (204 na doc de attach)
    """
    url = f"{NIBO_BASE}/files"
    files = {"file": (file_name, file_bytes)}
    r = requests.post(url, headers=nibo_headers(), files=files, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Falha no upload ({r.status_code}): {r.text}")
    # Algumas rotas do Nibo retornam corpo JSON; trate ambos os casos
    try:
        return r.json()
    except ValueError:
        # Sem JSON – tentar extrair FileId de header/location não documentado
        return {"raw": r.text}

def extract_file_id(upload_resp: dict) -> str:
    """
    Tenta encontrar o id do arquivo em respostas com formatos diferentes.
    """
    candidates = ["fileId", "FileId", "id", "Id", "ID"]
    for k in candidates:
        if isinstance(upload_resp, dict) and k in upload_resp and upload_resp[k]:
            return str(upload_resp[k])
    # às vezes a resposta vem aninhada
    for v in (upload_resp or {}).values():
        if isinstance(v, dict):
            fid = extract_file_id(v)
            if fid:
                return fid
    return ""

def attach_files_to_schedule(schedule_id: str, file_ids: list[str]) -> tuple[bool, str]:
    """
    Anexa arquivos ao agendamento de pagamento.
    Doc: POST /schedules/debit/{scheduleId}/files/attach  (retorna 204 se ok)
    Como o corpo não está 100% explícito no HTML, tentamos alguns formatos comuns.
    """
    url = f"{NIBO_BASE}/schedules/debit/{schedule_id}/files/attach"
    headers = nibo_headers() | {"Content-Type": "application/json"}

    payload_variants = [
        {"filesIds": file_ids},
        {"fileIds": file_ids},
        {"files": [{"fileId": fid} for fid in file_ids]},
        {"ids": file_ids},
    ]

    for payload in payload_variants:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        if r.status_code in (200, 201, 202, 204):
            return True, f"Anexado com sucesso (status {r.status_code}) com payload {payload}"
        # Se for 400 com mensagem clara, já devolve
        if r.status_code == 400 and "file" in (r.text or "").lower():
            return False, f"Erro 400 - verifique o payload ({payload}): {r.text}"

    return False, f"Falha ao anexar. Última resposta: {r.status_code} - {r.text}"

# =============== Streamlit UI ===============
st.set_page_config(page_title="Nibo - Upload & Anexar", page_icon="📎")
st.title("📎 Nibo — Upload de arquivos e anexar a agendamentos (pagamento)")

with st.sidebar:
    st.header("Configuração")
    st.write("Defina as variáveis de ambiente antes de rodar:\n- `NIBO_API_KEY`\n- opcional: `NIBO_USER_ID`")
    st.caption("A API do Nibo usa `X-API-Key` (e opcional `X-User-Id`).")

st.subheader("1) Upload de arquivos")
uploads = st.file_uploader("Selecione um ou mais arquivos", type=None, accept_multiple_files=True)

uploaded = []
if st.button("Fazer upload para o Nibo", disabled=not uploads):
    for up in uploads:
        try:
            resp = upload_file_to_nibo(up.name, up.getvalue())
            file_id = extract_file_id(resp)
            uploaded.append({"name": up.name, "fileId": file_id, "raw": resp})
        except Exception as e:
            st.error(f"Erro no upload de {up.name}: {e}")
    if uploaded:
        st.success("Upload concluído!")
        st.json(uploaded)

st.divider()
st.subheader("2) Anexar os arquivos a um agendamento de pagamento")

schedule_id = st.text_input("Informe o scheduleId do **pagamento** (debit)", placeholder="ex.: 8ca3961d-1800-480e-841d-27ebb6e0cbca")
# fonte de fileIds: prioriza os enviados na sessão atual
session_file_ids = [u["fileId"] for u in uploaded if u.get("fileId")]
file_ids_input = st.text_area(
    "IDs de arquivos (fileIds) — um por linha",
    value="\n".join(fid for fid in session_file_ids if fid),
    placeholder="cole aqui os fileIds caso já os tenha",
)

if st.button("Anexar ao agendamento", disabled=not schedule_id or not file_ids_input.strip()):
    file_ids = [line.strip() for line in file_ids_input.splitlines() if line.strip()]
    ok, msg = attach_files_to_schedule(schedule_id, file_ids)
    (st.success if ok else st.error)(msg)
