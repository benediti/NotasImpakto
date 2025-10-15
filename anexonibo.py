import os
import json
import requests
import streamlit as st
from datetime import date, datetime, timedelta
from dateutil.parser import parse as dtparse
from dotenv import load_dotenv
import re

# ================== Config Básica ==================
load_dotenv()  # carrega .env se existir
BASE = "https://api.nibo.com.br/empresas/v1"

st.set_page_config(page_title="Nibo: Upload + Filtros + Anexo", page_icon="📎", layout="wide")
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

def is_number(s: str) -> bool:
    return bool(re.fullmatch(r'\d+', s.strip()))

def get_stakeholder_name(item):
    """Extrai o nome do stakeholder (fornecedor/cliente) de um item"""
    return ((item.get("stakeholder") or {}).get("name")
        or (item.get("client") or {}).get("name")
        or (item.get("supplier") or {}).get("name")
        or "")

def get_due_date(item):
    """Extrai a data de vencimento do item e converte para objeto date"""
    due = item.get("dueDate") or item.get("due") or item.get("due_date") or ""
    if isinstance(due, str) and due:
        try:
            return dtparse(due).date()
        except:
            return None
    return None

def group_by_stakeholder(results):
    """Agrupa resultados por stakeholder (fornecedor/cliente)"""
    groups = {}
    for item in results:
        stakeholder = get_stakeholder_name(item)
        if not stakeholder:
            stakeholder = "Sem fornecedor/cliente"
        
        if stakeholder not in groups:
            groups[stakeholder] = []
        groups[stakeholder].append(item)
    
    return groups

def group_by_due_date(results):
    """Agrupa resultados por data de vencimento"""
    groups = {}
    for item in results:
        due_date = get_due_date(item)
        if not due_date:
            due_date_str = "Sem data"
        else:
            due_date_str = due_date.strftime("%d/%m/%Y")
        
        if due_date_str not in groups:
            groups[due_date_str] = []
        groups[due_date_str].append(item)
    
    return groups

def find_nf_number_in_string(text):
    """Extrai possíveis números de NF de um texto"""
    # Padrão para NF: números de 5-9 dígitos, podendo ter prefixos como NF:, NFe
    patterns = [
        r'NF:?\s*(\d{5,9})', # NF: 3126473
        r'NFe:?\s*(\d{5,9})', # NFe 3126473
        r'DANFE\s*(\d{5,9})', # DANFE 3126473
        r'Nota\s*Fiscal\s*:?\s*(\d{5,9})', # Nota Fiscal: 3126473
        r'(\d{9})', # Número de 9 dígitos
        r'(\d{6,8})', # Números de 6-8 dígitos
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            return matches[0]
    
    return None

def find_nf_number_in_filename(filename):
    """Extrai número de NF de um nome de arquivo"""
    # Padrão comum para arquivos de NF
    patterns = [
        r'0*(\d{5,9})', # 003126473
        r'NF0*(\d{5,9})', # NF3126473
        r'NFe0*(\d{5,9})', # NFe3126473
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, filename, re.IGNORECASE)
        if matches:
            return matches[0]
    
    return None

def calculate_match_score(schedule_item, filename, supplier_id=None):
    """
    Calcula pontuação de correspondência entre um agendamento e um arquivo
    Retorna: (pontuação, razão da correspondência)
    """
    score = 0
    reason = ""
    
    # Verifica se o fornecedor corresponde
    if supplier_id:
        schedule_supplier_id = (
            (schedule_item.get("stakeholder") or {}).get("id") or 
            (schedule_item.get("supplier") or {}).get("id")
        )
        if schedule_supplier_id == supplier_id:
            score += 30
            reason += "Fornecedor corresponde (+30). "
    
    # Extrai e compara números de NF
    description = schedule_item.get("description", "")
    nf_in_description = find_nf_number_in_string(description)
    nf_in_filename = find_nf_number_in_filename(filename)
    
    if nf_in_description and nf_in_filename and nf_in_description == nf_in_filename:
        score += 70
        reason += f"Número de NF ({nf_in_description}) encontrado em ambos (+70). "
    
    # Verifica palavras-chave comuns
    keywords = ["NF", "DANFE", "FATURA", "BOLETO", "RECIBO", "NOTA"]
    for keyword in keywords:
        if keyword in filename.upper() and keyword in description.upper():
            score += 10
            reason += f"Palavra-chave '{keyword}' encontrada em ambos (+10). "
    
    return score, reason.strip()

def auto_match_files_to_schedules(uploaded_files, schedules, supplier_id=None, threshold=50):
    """
    Encontra correspondências automáticas entre arquivos e agendamentos
    Retorna: lista de (file_id, schedule_id, score, reason)
    """
    matches = []
    
    for file in uploaded_files:
        best_match = None
        best_score = threshold
        best_reason = ""
        best_schedule = None
        
        for schedule in schedules:
            score, reason = calculate_match_score(schedule, file["name"], supplier_id)
            if score > best_score:
                sid = schedule.get("id") or schedule.get("scheduleId") or schedule.get("Id")
                best_match = sid
                best_score = score
                best_reason = reason
                best_schedule = schedule
        
        if best_match:
            matches.append({
                "file_id": file["id"],
                "file_name": file["name"],
                "schedule_id": best_match,
                "schedule_label": schedule_label(best_schedule),
                "score": best_score,
                "reason": best_reason
            })
    
    return matches

# ================== Estado ==================
if "uploaded_file_ids" not in st.session_state:
    st.session_state.uploaded_file_ids = []

if "last_results" not in st.session_state:
    st.session_state.last_results = []

if "pending_uploads" not in st.session_state:
    st.session_state.pending_uploads = []

if "selected_schedule_id" not in st.session_state:
    st.session_state.selected_schedule_id = None

if "uploaded_files" not in st.session_state:
    st.session_state.uploaded_files = []  # Lista de {id, name, size}

if "completed_attachments" not in st.session_state:
    st.session_state.completed_attachments = []  # Lista de {schedule_id, file_id}

if "supplier_id" not in st.session_state:
    st.session_state.supplier_id = "e00a5c53-3f79-4e37-8808-d9c8261daf7f"  # IMPAKTO SIST DE LIMPEZA E DESC LTDA

if "auto_matches" not in st.session_state:
    st.session_state.auto_matches = []  # Correspondências automáticas encontradas

# ================== Sidebar ==================
with st.sidebar:
    st.header("Configuração")
    st.write("Defina suas credenciais (em .env ou ambiente):")
    st.code("NIBO_API_TOKEN=SEU_TOKEN_AQUI", language="bash")
    st.caption("Usa header ApiToken (ou parâmetro apitoken).")
    
    # Limpar dados
    if st.button("🗑️ Limpar dados", use_container_width=True):
        st.session_state.uploaded_files = []
        st.session_state.pending_uploads = []
        st.session_state.completed_attachments = []
        st.session_state.auto_matches = []
        st.rerun()
    
    st.markdown("---")
    st.subheader("Conciliação automática")
    
    enable_auto_match = st.toggle("Habilitar conciliação automática", value=True)
    match_threshold = st.slider(
        "Limiar de correspondência", 
        min_value=30, 
        max_value=100,
        value=50,
        help="Pontuação mínima para considerar uma correspondência válida"
    )
    
    st.caption(f"Fornecedor fixo: IMPAKTO SIST DE LIMPEZA E DESC LTDA")
    
    if st.button("Limpar correspondências", key="clear_matches"):
        st.session_state.auto_matches = []
        st.rerun()

# ================== Layout principal com duas colunas ==================
col_search, col_upload = st.columns([3, 2])

# Coluna de busca e visualização de agendamentos
with col_search:
    st.subheader("Buscar agendamentos")
    
    # Botões de busca rápida
    st.markdown("### Busca rápida")
    quick_search_cols = st.columns(3)
    
    with quick_search_cols[0]:
        if st.button("📅 Hoje", use_container_width=True):
            today = date.today()
            with st.spinner("Buscando agendamentos de hoje..."):
                try:
                    odata_filter = f"dueDate ge {today.isoformat()} and dueDate le {today.isoformat()}"
                    results = list_schedules("debit", True, top=100, odata_filter=odata_filter)
                    st.session_state.last_results = results
                    st.session_state.kind_key = "debit"
                    st.session_state.group_by = "data"
                    st.success(f"Encontrados {len(results)} agendamentos para hoje")
                except Exception as e:
                    st.error(f"Erro: {str(e)}")
    
    with quick_search_cols[1]:
        if st.button("📅 Esta semana", use_container_width=True):
            today = date.today()
            start_of_week = today - timedelta(days=today.weekday())
            end_of_week = start_of_week + timedelta(days=6)
            with st.spinner("Buscando agendamentos desta semana..."):
                try:
                    odata_filter = f"dueDate ge {start_of_week.isoformat()} and dueDate le {end_of_week.isoformat()}"
                    results = list_schedules("debit", True, top=100, odata_filter=odata_filter)
                    st.session_state.last_results = results
                    st.session_state.kind_key = "debit"
                    st.session_state.group_by = "data"
                    st.success(f"Encontrados {len(results)} agendamentos para esta semana")
                except Exception as e:
                    st.error(f"Erro: {str(e)}")
    
    with quick_search_cols[2]:
        if st.button("📅 Este mês", use_container_width=True):
            today = date.today()
            start_of_month = date(today.year, today.month, 1)
            if today.month == 12:
                end_of_month = date(today.year+1, 1, 1) - timedelta(days=1)
            else:
                end_of_month = date(today.year, today.month+1, 1) - timedelta(days=1)
            
            with st.spinner("Buscando agendamentos deste mês..."):
                try:
                    odata_filter = f"dueDate ge {start_of_month.isoformat()} and dueDate le {end_of_month.isoformat()}"
                    results = list_schedules("debit", True, top=100, odata_filter=odata_filter)
                    st.session_state.last_results = results
                    st.session_state.kind_key = "debit"
                    st.session_state.group_by = "data"
                    st.success(f"Encontrados {len(results)} agendamentos para este mês")
                except Exception as e:
                    st.error(f"Erro: {str(e)}")
    
    # Busca personalizada
    st.markdown("### Busca personalizada")
    
    col1, col2 = st.columns(2)
    with col1:
        kind = st.radio("Tipo", options=("Pagamentos (debit)", "Recebimentos (credit)"), horizontal=True)
        kind_key = "debit" if kind.startswith("Pagamentos") else "credit"
        st.session_state.kind_key = kind_key
    
    with col2:
        group_by = st.radio("Agrupar por", options=("Data", "Fornecedor/Cliente"), horizontal=True)
        st.session_state.group_by = "data" if group_by == "Data" else "fornecedor"
    
    # Filtros em linha
    col_desc, col_toggle = st.columns([4, 1])
    with col_desc:
        desc_contains = st.text_input("Buscar por descrição ou número", value="", placeholder="Ex: 3344, NF, pagamento")
    with col_toggle:
        opened_only = st.toggle("Apenas abertos", value=True)
    
    # Datas
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        d_start = st.date_input("Data inicial", value=None)
    with col_d2:
        d_end = st.date_input("Data final", value=None)
    
    # Botão de busca
    if st.button("🔍 Buscar agendamentos", use_container_width=True):
        with st.spinner("Buscando agendamentos..."):
            try:
                odata_from_ui = build_odata_filter(
                    d_start if isinstance(d_start, date) else None,
                    d_end if isinstance(d_end, date) else None,
                    None,
                    desc_contains if desc_contains.strip() else None,
                    None, None
                )
                
                results = list_schedules(kind_key, opened_only, top=100, odata_filter=odata_from_ui)
                
                # Filtra resultados se tiver um número específico na descrição
                if desc_contains.strip() and is_number(desc_contains.strip()):
                    results = [r for r in results if desc_contains.strip() in (r.get("description") or "")]
                
                st.session_state.last_results = results
                
                if not results:
                    st.warning("Nenhum agendamento encontrado com esses critérios.")
                else:
                    st.success(f"Encontrados {len(results)} agendamentos.")
            except Exception as e:
                st.error(f"Erro na busca: {str(e)}")
    
    # Exibição dos resultados agrupados
    if st.session_state.last_results:
        st.markdown("---")
        
        if st.session_state.group_by == "data":
            groups = group_by_due_date(st.session_state.last_results)
            st.markdown("### Agendamentos agrupados por data")
        else:
            groups = group_by_stakeholder(st.session_state.last_results)
            st.markdown("### Agendamentos agrupados por fornecedor/cliente")
        
        # Mostra cada grupo em um expander
        for group_name, items in groups.items():
            with st.expander(f"{group_name} ({len(items)} agendamentos)"):
                for item in items:
                    lbl = schedule_label(item)
                    sid = item.get("id") or item.get("scheduleId") or item.get("Id") or item.get("ScheduleId")
                    
                    # Verifica se há anexos pendentes para este agendamento
                    pending_files = [f for f in st.session_state.uploaded_files 
                                     if not any(a["schedule_id"] == sid and a["file_id"] == f["id"] 
                                               for a in st.session_state.completed_attachments)]
                    
                    with st.container(border=True):
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.write(f"**{lbl}**")
                        
                        with col2:
                            # Anexar arquivo diretamente
                            if pending_files:
                                file_options = {f["name"]: f["id"] for f in pending_files}
                                selected_file = st.selectbox(
                                    "Arquivo", 
                                    options=list(file_options.keys()),
                                    key=f"select_file_{sid}"
                                )
                                
                                if st.button("Anexar", key=f"btn_attach_{sid}"):
                                    file_id = file_options[selected_file]
                                    try:
                                        ok, msg = attach_files(
                                            st.session_state.kind_key,
                                            sid,
                                            [file_id]
                                        )
                                        
                                        if ok:
                                            # Adiciona ao histórico de anexações
                                            st.session_state.completed_attachments.append({
                                                "schedule_id": sid,
                                                "file_id": file_id,
                                                "file_name": selected_file,
                                                "attached_at": datetime.now().isoformat()
                                            })
                                            
                                            # Remove o arquivo da lista de disponíveis
                                            st.session_state.uploaded_files = [
                                                f for f in st.session_state.uploaded_files 
                                                if f["id"] != file_id
                                            ]
                                            
                                            st.success("✅ Anexado com sucesso!")
                                            st.rerun()
                                        else:
                                            st.error(f"Erro: {msg}")
                                    except Exception as e:
                                        st.error(f"Erro: {str(e)}")
                            else:
                                st.info("Sem arquivos pendentes")

# Coluna de upload de arquivos
with col_upload:
    st.subheader("Upload e arquivos")
    
    # Upload de arquivos
    uploaded_files = st.file_uploader(
        "Selecione um ou mais arquivos",
        type=None,
        accept_multiple_files=True,
        key="file_uploader_main"
    )
    
    if uploaded_files:
        # Adiciona apenas arquivos novos à lista de pendentes
        for up in uploaded_files:
            if up.name not in [f.name for f in st.session_state.pending_uploads]:
                st.session_state.pending_uploads.append(up)
    
    # Arquivos pendentes para upload
    if st.session_state.pending_uploads:
        st.markdown("### Arquivos pendentes")
        
        for idx, up in enumerate(st.session_state.pending_uploads[:]):
            with st.container(border=True):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(f"{up.name} ({up.size/1024:.1f} KB)")
                with col2:
                    if st.button("Upload", key=f"btn_upload_{idx}"):
                        with st.spinner(f"Enviando {up.name}..."):
                            try:
                                resp = upload_file_to_nibo(up.name, up.getvalue(), up.type)
                                fid = extract_file_id(resp)
                                if fid:
                                    # Adiciona aos arquivos disponíveis
                                    file_info = {  # Define file_info aqui antes de usá-lo
                                        "id": fid,
                                        "name": up.name,
                                        "size": up.size,
                                        "uploaded_at": datetime.now().isoformat()
                                    }
                                    st.session_state.uploaded_files.append(file_info)
                                    
                                    # Tenta fazer correspondência automática se habilitado
                                    if enable_auto_match and st.session_state.last_results:
                                        matches = auto_match_files_to_schedules(
                                            [file_info],  # Agora file_info já está definido
                                            st.session_state.last_results,
                                            st.session_state.supplier_id,
                                            match_threshold
                                        )
                                        if matches:
                                            st.session_state.auto_matches.extend(matches)
                                    
                                    # Remove dos pendentes
                                    st.session_state.pending_uploads.remove(up)
                                    st.success(f"Upload concluído: {up.name}")
                                    st.rerun()
                            except Exception as e:
                                st.error(f"Erro no upload: {str(e)}")
    
    # Arquivos disponíveis para anexar
    if st.session_state.uploaded_files:
        st.markdown("### Arquivos disponíveis para anexar")
        
        for idx, file in enumerate(st.session_state.uploaded_files):
            # Verifica se o arquivo já foi anexado a algum agendamento
            attachments = [a for a in st.session_state.completed_attachments 
                          if a["file_id"] == file["id"]]
            
            # Se já foi anexado a todos os agendamentos, não mostra na lista de disponíveis
            if len(attachments) > 0 and len(st.session_state.last_results) == len(attachments):
                continue
                
            with st.container(border=True):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.write(f"{file['name']} ({file['size']/1024:.1f} KB)")
                    if attachments:
                        st.caption(f"Anexado a {len(attachments)} agendamentos")
                
                with col2:
                    if not st.session_state.last_results:
                        st.info("Busque agendamentos")
                    else:
                        # Permite selecionar um agendamento para anexar
                        schedule_options = {}
                        for item in st.session_state.last_results:
                            sid = item.get("id") or item.get("scheduleId")
                            # Verifica se este arquivo já foi anexado a este agendamento
                            if not any(a["schedule_id"] == sid and a["file_id"] == file["id"] 
                                      for a in st.session_state.completed_attachments):
                                schedule_options[schedule_label(item)] = sid
                        
                        if schedule_options:
                            selected_schedule = st.selectbox(
                                "Anexar a", 
                                options=list(schedule_options.keys()),
                                key=f"select_schedule_{file['id']}"
                            )
                            
                            if st.button("Anexar", key=f"attach_btn_{file['id']}"):
                                sid = schedule_options[selected_schedule]
                                try:
                                    ok, msg = attach_files(
                                        st.session_state.kind_key,
                                        sid,
                                        [file["id"]]
                                    )
                                    
                                    if ok:
                                        # Adiciona ao histórico de anexações
                                        st.session_state.completed_attachments.append({
                                            "schedule_id": sid,
                                            "file_id": file["id"],
                                            "file_name": file["name"],
                                            "schedule_label": selected_schedule,
                                            "attached_at": datetime.now().isoformat()
                                        })
                                        
                                        # Remove o arquivo da lista de disponíveis
                                        st.session_state.uploaded_files = [
                                            f for f in st.session_state.uploaded_files 
                                            if f["id"] != file["id"]
                                        ]
                                        
                                        st.success("✅ Anexado com sucesso!")
                                        st.rerun()
                                    else:
                                        st.error(f"Erro: {msg}")
                                except Exception as e:
                                    st.error(f"Erro: {str(e)}")
                        else:
                            st.success("Anexado a todos")
    
    # Histórico de anexações
    if st.session_state.completed_attachments:
        with st.expander("Histórico de anexações"):
            for idx, attachment in enumerate(st.session_state.completed_attachments):
                st.write(f"- {attachment['file_name']} → {attachment.get('schedule_label', 'Agendamento')}")
            
            if st.button("Limpar histórico", key="btn_clear_history_1"):  # ← Adicione uma key única aqui
                st.session_state.completed_attachments = []
                st.rerun()

# Rodapé
st.divider()
st.caption("Ferramenta de anexação de arquivos ao Nibo • Selecione agendamentos, faça upload de arquivos e anexe-os facilmente")

# Adicione uma seção para correspondências automáticas após os arquivos disponíveis
if st.session_state.auto_matches:
    st.markdown("### Correspondências automáticas encontradas")
    
    # Ordena por pontuação (maior primeiro)
    sorted_matches = sorted(st.session_state.auto_matches, key=lambda x: x["score"], reverse=True)
    
    for idx, match in enumerate(sorted_matches):
        if any(a["file_id"] == match["file_id"] for a in st.session_state.completed_attachments):
            continue  # Pula se já anexado
            
        with st.container(border=True):
            col1, col2 = st.columns([4, 1])
            with col1:
                st.write(f"**{match['file_name']}**")
                st.write(f"📄 Corresponde a: {match['schedule_label']}")
                st.caption(f"Confiança: {match['score']}% • {match['reason']}")
            with col2:
                if st.button("Confirmar", key=f"confirm_match_{idx}"):
                    try:
                        ok, msg = attach_files(
                            st.session_state.kind_key,
                            match["schedule_id"],
                            [match["file_id"]]
                        )
                        
                        if ok:
                            # Adiciona ao histórico de anexações
                            st.session_state.completed_attachments.append({
                                "schedule_id": match["schedule_id"],
                                "file_id": match["file_id"],
                                "file_name": match["file_name"],
                                "schedule_label": match["schedule_label"],
                                "attached_at": datetime.now().isoformat(),
                                "auto_matched": True
                            })
                            
                            # Remove o arquivo da lista de disponíveis
                            st.session_state.uploaded_files = [
                                f for f in st.session_state.uploaded_files 
                                if f["id"] != match["file_id"]
                            ]
                            
                            st.success("✅ Anexado com sucesso!")
                            st.rerun()
                        else:
                            st.error(f"Erro: {msg}")
                    except Exception as e:
                        st.error(f"Erro: {str(e)}")

# Adicione na seção de histórico um indicador visual para correspondências automáticas
if st.session_state.completed_attachments:
    with st.expander("Histórico de anexações"):
        for idx, attachment in enumerate(st.session_state.completed_attachments):
            auto_matched = "🤖 " if attachment.get("auto_matched") else ""
            st.write(f"- {auto_matched}{attachment['file_name']} → {attachment.get('schedule_label', 'Agendamento')}")
        
        if st.button("Limpar histórico", key="btn_clear_history_2"):  # ← Adicione uma key única aqui
            st.session_state.completed_attachments = []
            st.rerun()
