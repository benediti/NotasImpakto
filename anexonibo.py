import os
import json
import requests
import streamlit as st
from datetime import date, datetime
from dateutil.parser import parse as dtparse
from dotenv import load_dotenv
import re

# ================== Config Básica ==================
load_dotenv()  # carrega .env se existir
BASE = "https://api.nibo.com.br/empresas/v1"

st.set_page_config(page_title="Nibo: Upload + Filtros + Anexo", page_icon="📎")
st.title("📎 Nibo — Upload, filtros e anexo em agendamentos")

# ================== Helpers ==================
def nibo_headers(json_body: bool = False) -> dict:
    """
    Preferimos o header 'ApiToken' (ou param apitoken na URL).
    """
    api_token = os.environ.get("NIBO_API_TOKEN") or os.environ.get("NIBO_API_KEY") or ""
    if not api_token:
        st.warning("Defina NIBO_API_TOKEN (ou NIBO_API_KEY) no ambiente ou em um arquivo .env")
    h = {"ApiToken": api_token, "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h

def upload_file_to_nibo(file_name: str, file_bytes: bytes, content_type: str = None) -> dict:
    url = f"{BASE}/files"
    # Inclui o content_type se informado
    if content_type:
        files = {"file": (file_name, file_bytes, content_type)}
    else:
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
    if isinstance(upload_resp, dict):
        for v in upload_resp.values():
            if isinstance(v, dict) or isinstance(v, list):
                fid = extract_file_id(v)
                if fid:
                    return fid
    elif isinstance(upload_resp, list):
        for item in upload_resp:
            fid = extract_file_id(item)
            if fid:
                return fid
    return ""

def schedule_label(it: dict) -> str:
    sid = it.get("id") or it.get("scheduleId") or it.get("Id") or it.get("ScheduleId") or ""
    desc = it.get("description") or it.get("title") or ""
    due = it.get("dueDate") or it.get("due") or it.get("due_date") or ""
    val = it.get("value") or it.get("amount") or ""
    stakeholder = (
        (it.get("stakeholder") or {}).get("name")
        or (it.get("client") or {}).get("name")
        or (it.get("supplier") or {}).get("name")
        or ""
    )
    parts = []
    if isinstance(due, (int, float)): due = str(due)
    if due: parts.append(str(due))
    if desc: parts.append(str(desc))
    if stakeholder: parts.append(f"({stakeholder})")
    if val: parts.append(f"R$ {val}")
    if sid: parts.append(f"[{sid}]")
    return " • ".join([p for p in parts if p])

def _escape_odata_string(s: str) -> str:
    return s.replace("'", "''")

def build_odata_filter(d_start: date | None, d_end: date | None,
                       stakeholder_name: str | None,
                       desc_contains: str | None,
                       min_value: float | None,
                       max_value: float | None) -> str:
    """
    Monta um $filter OData básico usando campos comuns:
      - dueDate ge/le
      - contains(description,'...')
      - contains(stakeholder/name,'...')
      - value ge/le
    Observação: caso algum campo não exista exatamente no seu tenant, o servidor ignora ou retorna 400.
    """
    clauses = []
    if d_start:
        # padroniza para ISO yyyy-mm-dd
        clauses.append(f"dueDate ge {d_start.isoformat()}")
    if d_end:
        clauses.append(f"dueDate le {d_end.isoformat()}")

    if desc_contains:
        s = _escape_odata_string(desc_contains.strip())
        # usamos tolower por segurança, mas nem todo servidor OData aceita: deixamos sem função
        clauses.append(f"contains(description,'{s}')")

    if stakeholder_name:
        s = _escape_odata_string(stakeholder_name.strip())
        # tentamos vários campos comuns (stakeholder/name, client/name, supplier/name)
        name_clauses = [
            f"contains(stakeholder/name,'{s}')",
            f"contains(client/name,'{s}')",
            f"contains(supplier/name,'{s}')",
        ]
        clauses.append("(" + " or ".join(name_clauses) + ")")

    if min_value is not None:
        clauses.append(f"value ge {min_value}")
    if max_value is not None:
        clauses.append(f"value le {max_value}")

    return " and ".join(clauses)

def list_schedules(kind: str, opened_only: bool, top: int = 100,
                   orderby: str = "dueDate desc",
                   odata_filter: str | None = None) -> list[dict]:
    assert kind in ("debit", "credit")
    base_path = f"/schedules/{kind}/opened" if opened_only else f"/schedules/{kind}"
    url = f"{BASE}{base_path}"
    params = {"$orderby": orderby, "$top": str(top)}
    if odata_filter and odata_filter.strip():
        params["$filter"] = odata_filter
    r = requests.get(url, headers=nibo_headers(), params=params, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"Erro ao listar {kind} ({'abertos' if opened_only else 'todos'}) — {r.status_code}: {r.text}")
    data = r.json()
    if isinstance(data, dict) and "items" in data:
        return data["items"] or []
    if isinstance(data, list):
        return data
    return data.get("value") or data.get("results") or []

def attach_files(kind: str, schedule_id: str, file_ids: list[str]) -> tuple[bool, str]:
    assert kind in ("debit", "credit")
    url = f"{BASE}/schedules/{kind}/{schedule_id}/files/attach"
    headers = nibo_headers(json_body=True)

    # O corpo deve ser apenas uma lista de strings (IDs)
    payload = file_ids
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
    if r.status_code in (200, 201, 202, 204):
        return True, f"Anexado com sucesso (status {r.status_code})"
    return False, f"Falha ao anexar: status {r.status_code} • resposta: {r.text} • payload: {payload}"

def has_number(s: str) -> bool:
    """Retorna True se a string contém pelo menos um número."""
    return bool(re.search(r'\d+', s or ""))

# ================== Sidebar ==================
with st.sidebar:
    st.header("Configuração")
    st.write("Defina suas credenciais (em .env ou ambiente):")
    st.code("NIBO_API_TOKEN=SEU_TOKEN_AQUI", language="bash")
    st.caption("Usa header ApiToken (ou parâmetro apitoken).")

# ================== Estado ==================
if "uploaded_file_ids" not in st.session_state:
    st.session_state.uploaded_file_ids = []

if "last_results" not in st.session_state:
    st.session_state.last_results = []

# ================== 1) Upload ==================
st.subheader("1) Upload de arquivos")
uploads = st.file_uploader("Selecione 1+ arquivos", type=None, accept_multiple_files=True)
if st.button("Fazer upload"):
    if not uploads:
        st.warning("Selecione pelo menos um arquivo.")
    else:
        saved = []
        for up in uploads:
            try:
                # Passa o tipo do arquivo para a função
                resp = upload_file_to_nibo(up.name, up.getvalue(), up.type)
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

# ================== 2) Filtros & Busca ==================
if "show_cards_only" not in st.session_state:
    st.session_state.show_cards_only = False

if not st.session_state.show_cards_only:
    st.subheader("2) Buscar agendamentos")
    col_kind, col_scope = st.columns(2)
    with col_kind:
        kind = st.radio("Tipo", options=("Pagamentos (debit)", "Recebimentos (credit)"), horizontal=True)
        kind_key = "debit" if kind.startswith("Pagamentos") else "credit"
        st.session_state.kind_key = kind_key
    with col_scope:
        opened_only = st.toggle("Listar apenas abertos", value=True, help="Desative para listar TODOS")

    col_top, col_order = st.columns(2)
    with col_top:
        top = st.number_input("Quantidade (top)", min_value=1, max_value=500, value=100, step=1)
    with col_order:
        order = st.text_input("Ordenação ($orderby)", value="dueDate desc")

    # --- Filtros prontos ---
    st.markdown("**Filtros rápidos** (opcional)")
    col_d1, col_d2, col_min, col_max = st.columns(4)
    with col_d1:
        d_start = st.date_input("Data inicial (dueDate ≥)", value=None, format="YYYY-MM-DD")
    with col_d2:
        d_end = st.date_input("Data final (dueDate ≤)", value=None, format="YYYY-MM-DD")
    with col_min:
        min_val_str = st.text_input("Valor mínimo", value="")
    with col_max:
        max_val_str = st.text_input("Valor máximo", value="")

    desc_contains = st.text_input("Descrição contém", value="")

    # Parse min_val and max_val from input strings
    try:
        min_val = float(min_val_str) if min_val_str.strip() else None
    except ValueError:
        min_val = None
    try:
        max_val = float(max_val_str) if max_val_str.strip() else None
    except ValueError:
        max_val = None

    def is_number(s: str) -> bool:
        return bool(re.fullmatch(r'\d+', s.strip()))

    # ================== 2) Filtros & Busca (continuação) ==================
    if st.button("Buscar"):
        try:
            # Se o campo de descrição contém só número, faz o filtro normalmente
            # Define stakeholder_free como string vazia, pois não há campo de filtro livre para stakeholder
            stakeholder_free = ""
            if desc_contains.strip() and is_number(desc_contains.strip()):
                odata_from_ui = build_odata_filter(
                    d_start if isinstance(d_start, date) else None,
                    d_end if isinstance(d_end, date) else None,
                    stakeholder_free if stakeholder_free.strip() else None,
                    desc_contains,
                    min_val, max_val
                )
                final_filter = odata_from_ui
            else:
                odata_from_ui = build_odata_filter(
                    d_start if isinstance(d_start, date) else None,
                    d_end if isinstance(d_end, date) else None,
                    stakeholder_free if stakeholder_free.strip() else None,
                    desc_contains if desc_contains.strip() else None,
                    min_val, max_val
                )

                # Defina odatabuilder_extra como string vazia (ou ajuste conforme sua lógica)
                odatabuilder_extra = ""

                final_filter = ""
                if odata_from_ui and odatabuilder_extra:
                    final_filter = f"({odata_from_ui}) and ({odatabuilder_extra})"
                elif odata_from_ui:
                    final_filter = odata_from_ui
                elif odatabuilder_extra:
                    final_filter = odatabuilder_extra

            results = list_schedules(kind_key, opened_only, top=top, orderby=order, odata_filter=final_filter)
            st.session_state.last_results = results or []
            st.session_state.show_cards_only = True
            if not results:
                st.info("Nenhum agendamento encontrado com esse número na descrição.")
            else:
                st.success(f"Encontrados {len(results)} agendamentos.")
        except Exception as e:
            st.error(str(e))
else:
    if st.button("Voltar para filtros"):
        st.session_state.show_cards_only = False

# ================== 2.1) Autocomplete de Fornecedor/Cliente ==================
if st.session_state.last_results:
    # extrai nomes únicos de stakeholder
    names = set()
    for it in st.session_state.last_results:
        for k in ("stakeholder", "client", "supplier"):
            obj = it.get(k) or {}
            nm = obj.get("name")
            if nm:
                names.add(nm)
    names_list = sorted(names)
    if names_list:
        st.markdown("**Autocomplete de Fornecedor/Cliente** (aplica um contains no nome)")
        selected_name = st.selectbox("Escolha um nome para filtrar novamente", options=["(não filtrar)"] + names_list)
        if selected_name != "(não filtrar)":
            extra_name_filter = build_odata_filter(
                d_start if isinstance(d_start, date) else None,
                d_end if isinstance(d_end, date) else None,
                selected_name,  # usa o nome selecionado
                desc_contains if desc_contains.strip() else None,
                min_val, max_val
            )
            # combina com filtro avançado, se houver
            if odatabuilder_extra:
                extra_name_filter = f"({extra_name_filter}) and ({odatabuilder_extra})"
            try:
                results = list_schedules(kind_key, opened_only, top=top, orderby=order, odata_filter=extra_name_filter)
                st.session_state.last_results = results or []
                st.success(f"Refinado por fornecedor/cliente: {selected_name} — {len(results)} resultados.")
                st.json({"preview": results[:3]})
            except Exception as e:
                st.error(str(e))

# ================== 2.2) Escolha do agendamento como cards ==================
options = []
id_map = {}
for it in st.session_state.last_results:
    lbl = schedule_label(it)
    sid = it.get("id") or it.get("scheduleId") or it.get("Id") or it.get("ScheduleId")
    if sid:
        options.append(lbl or sid)
        id_map[lbl or sid] = sid

st.markdown("### Agendamentos encontrados")
num_cols = 3  # Quantidade de cards por linha
rows = [options[i:i+num_cols] for i in range(0, len(options), num_cols)]

if "selected_label" not in st.session_state:
    st.session_state.selected_label = None
if "selected_schedule_id" not in st.session_state:
    st.session_state.selected_schedule_id = None

for row in rows:
    cols = st.columns(len(row))
    for idx, lbl in enumerate(row):
        sid = id_map[lbl]
        with cols[idx]:
            st.markdown(f"**{lbl}**")
            if st.button("Selecionar", key=f"card_{sid}"):
                st.session_state.selected_label = lbl
                st.session_state.selected_schedule_id = sid
            # Destaca o card selecionado
            if st.session_state.selected_label == lbl:
                st.success("Selecionado")

selected_schedule_id = st.session_state.selected_schedule_id

st.divider()

# ================== 3) Anexar ==================
st.subheader("3) Anexar arquivos ao agendamento selecionado")
st.caption("Use os FileIds recém-enviados ou cole manualmente (um por linha).")

preset = "\n".join(st.session_state.uploaded_file_ids) if st.session_state.uploaded_file_ids else ""
file_ids_input = st.text_area("FileIds", value=preset, placeholder="FILE_ID_1\nFILE_ID_2")

can_attach = bool(selected_schedule_id and file_ids_input.strip())
if st.button("Anexar agora", disabled=not can_attach):
    file_ids = [l.strip() for l in file_ids_input.splitlines() if l.strip()]
    file_ids = [fid for fid in file_ids if fid]
    if not file_ids:
        st.error("Nenhum FileId válido informado.")
    else:
        ok, msg = attach_files(
            st.session_state.kind_key if "kind_key" in st.session_state else "debit",
            selected_schedule_id,
            file_ids
        )
        (st.success if ok else st.error)(msg)



st.caption("Dica: aumente o 'top' para ver mais itens; para paginação avançada, use $skiptoken se seu endpoint suportar.")
st.caption("Dica: aumente o 'top' para ver mais itens; para paginação avançada, use $skiptoken se seu endpoint suportar.")
