import os
import json
import requests
import streamlit as st
from datetime import date

BASE = "https://api.nibo.com.br/empresas/v1"

# ---------- Helpers de autenticação ----------
def nibo_headers(json_body: bool = False) -> dict:
    """
    Empresa API do Nibo aceita o token como header 'ApiToken' (ou via query 'apitoken').
    Preferimos o header, conforme doc oficial.
    """
    api_token = os.environ.get("NIBO_API_TOKEN") or os.environ.get("NIBO_API_KEY") or ""
    if not api_token:
        st.warning("Defina a variável de ambiente NIBO_API_TOKEN (ou NIBO_API_KEY).")
    h = {"ApiToken": api_token, "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h

# ---------- Upload ----------
def upload_file_to_nibo(file_name: str, file_bytes: bytes) -> dict:
    url = f"{BASE}/files"
    files = {"file": (file_name, file_bytes)}
    r = requests.post(url, headers=nibo_headers(), files=files, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Falha no upload ({r.status_code}): {r.text}")
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text}

def extract_file_id(upload_resp: dict) -> str:
    for k in ("FileId", "fileId", "id", "Id", "ID"):
        if isinstance(upload_resp, dict) and upload_resp.get(k):
            return str(upload_resp[k])
    # nested
    for v in (upload_resp or {}).values():
        if isinstance(v, dict):
            fid = extract_file_id(v)
            if fid:
                return fid
    return ""

# ---------- Listagens (abertos) ----------
def list_open_schedules(kind: str, top: int = 50, orderby: str = "dueDate desc", extra_filter: str = "") -> list[dict]:
    """
    kind: 'debit' (pagamentos) ou 'credit' (recebimentos)
    Usa endpoints /schedules/{kind}/opened com OData ($orderby, $top e $filter).
    """
    assert kind in ("debit", "credit")
    url = f"{BASE}/schedules/{kind}/opened"
    params = {"$orderby": orderby, "$top": str(top)}
    if extra_filter.strip():
        params["$filter"] = extra_filter
    r = requests.get(url, headers=nibo_headers(), params=params, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Erro ao listar {kind} abertos ({r.status_code}): {r.text}")
    data = r.json()
    # respostas do Nibo geralmente trazem 'items' ou lista direta; lidamos com ambos
    if isinstance(data, dict) and "items" in data:
        return data["items"] or []
    if isinstance(data, list):
        return data
    # fallback
    return data.get("value") or data.get("results") or []

def schedule_label(it: dict) -> str:
    """
    Cria um rótulo amigável para exibir no select (tenta pegar campos comuns).
    """
    sid = it.get("id") or it.get("scheduleId") or it.get("Id") or it.get("ScheduleId") or ""
    desc = it.get("description") or it.get("title") or ""
    due = it.get("dueDate") or it.get("due") or it.get("due_date") or ""
    val = it.get("value") or it.get("amount") or ""
    # stakeholder aninhado costuma vir como stakeholder/name, tentamos algumas chaves
    stakeholder = (
        (it.get("stakeholder") or {}).get("name")
        or (it.get("client") or {}).get("name")
        or (it.get("supplier") or {}).get("name")
        or ""
    )
    # string enxuta
    parts = []
    if due: parts.append(str(due))
    if desc: parts.append(str(desc))
    if stakeholder: parts.append(f"({stakeholder})")
    if val: parts.append(f"R$ {val}")
    if sid: parts.append(f"[{sid}]")
    return " • ".join([p for p in parts if p])

# ---------- Attach ----------
def attach_files(kind: str, schedule_id: str, file_ids: list[str]) -> tuple[bool, str]:
    """
    Anexa arquivos no agendamento (pagamento=debit ou recebimento=credit).
    Doc: /schedules/debit/{scheduleId}/files/attach e /schedules/credit/{scheduleId}/files/attach
    """
    assert kind in ("debit", "credit")
    url = f"{BASE}/schedules/{kind}/{schedule_id}/files/attach"
    headers = nibo_headers(json_body=True)

    # A doc não exibe explicitamente o corpo, então tentamos variantes comuns.
    variants = [
        {"fileIds": file_ids},
        {"filesIds": file_ids},  # algumas páginas usam essa grafia
        {"files": [{"fileId": fid} for fid in file_ids]},
        {"ids": file_ids},
    ]

    last = None
    for payload in variants:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        last = (r.status_code, r.text, payload)
        if r.status_code in (200, 201, 202, 204):
            return True, f"Anexado com sucesso (status {r.status_code}) com payload {payload}"
    return False, f"Falha ao anexar: status {last[0]} • resposta: {last[1]} • último payload testado: {last[2]}"

# ---------- UI ----------
st.set_page_config(page_title="Nibo: Upload & Anexar com seleção de agendamento", page_icon="📎")
st.title("📎 Nibo — Upload, listar abertos e anexar")

with st.sidebar:
    st.header("Configuração")
    st.write("Defina no seu ambiente:")
    st.code("export NIBO_API_TOKEN='SEU_TOKEN_AQUI'", language="bash")
    st.caption("Conforme a doc, use o header ApiToken (ou param apitoken na URL).")

# Sessão para manter uploads feitos agora
if "uploaded_file_ids" not in st.session_state:
    st.session_state.uploaded_file_ids = []

# 1) Upload
st.subheader("1) Upload de arquivos")
uploads = st.file_uploader("Selecione 1+ arquivos", type=None, accept_multiple_files=True)
if st.button("Fazer upload"):
    if not uploads:
        st.warning("Selecione pelo menos um arquivo.")
    else:
        saved = []
        for up in uploads:
            try:
                resp = upload_file_to_nibo(up.name, up.getvalue())
                fid = extract_file_id(resp)
                saved.append({"name": up.name, "fileId": fid, "raw": resp})
                if fid:
                    st.session_state.uploaded_file_ids.append(fid)
            except Exception as e:
                st.error(f"Erro no upload de {up.name}: {e}")
        if saved:
            st.success("Upload concluído!")
            st.json(saved)

st.divider()

# 2) Buscar agendamentos em aberto
st.subheader("2) Buscar agendamentos em aberto")
kind = st.radio("Tipo de agendamento", options=("Pagamentos (debit)", "Recebimentos (credit)"), horizontal=True)
kind_key = "debit" if kind.startswith("Pagamentos") else "credit"

col_a, col_b = st.columns(2)
with col_a:
    top = st.number_input("Quantidade (top)", min_value=1, max_value=500, value=50, step=1)
with col_b:
    order = st.text_input("Ordenação ($orderby)", value="dueDate desc", help="Ex.: dueDate desc, createDate desc, value asc")

odata_filter = st.text_input(
    "Filtro OData opcional ($filter)",
    value="",
    placeholder="ex.: year(dueDate) eq 2025 AND month(dueDate) eq 10 AND value ge 100"
)

results = []
if st.button("Buscar agendamentos abertos"):
    try:
        results = list_open_schedules(kind_key, top=top, orderby=order, extra_filter=odata_filter)
        if not results:
            st.info("Nenhum agendamento encontrado com esses critérios.")
        else:
            st.success(f"Encontrados {len(results)} agendamentos.")
            st.json({"preview": results[:3]})  # mostra amostra para inspecionar campos
    except Exception as e:
        st.error(str(e))

# Construir lista de escolhas
options = []
id_map = {}
for it in results:
    lbl = schedule_label(it)
    sid = it.get("id") or it.get("scheduleId") or it.get("Id") or it.get("ScheduleId")
    if sid:
        options.append(lbl or sid)
        id_map[lbl or sid] = sid

selected_label = st.selectbox("Escolha um agendamento", options=options) if options else None
selected_schedule_id = id_map.get(selected_label or "", "")

st.divider()

# 3) Anexar
st.subheader("3) Anexar arquivos ao agendamento selecionado")
st.caption("Use os FileIds recém enviados ou cole manualmente um por linha.")

preset = "\n".join(st.session_state.uploaded_file_ids) if st.session_state.uploaded_file_ids else ""
file_ids_input = st.text_area("FileIds (um por linha)", value=preset)

can_attach = bool(selected_schedule_id and file_ids_input.strip())
if st.button("Anexar agora", disabled=not can_attach):
    file_ids = [l.strip() for l in file_ids_input.splitlines() if l.strip()]
    ok, msg = attach_files(kind_key, selected_schedule_id, file_ids)
    (st.success if ok else st.error)(msg)

st.caption("Dica: se quiser listar **todos** os agendamentos (não só abertos), você pode usar os endpoints /schedules/debit e /schedules/credit com OData.")
