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

# ================== Funções para nova sessão ==================
def nova_sessao():
    """Limpa o estado para iniciar uma nova conciliação"""
    st.session_state.selected_label = None
    st.session_state.selected_schedule_id = None
    st.session_state.pending_uploads = []
    st.session_state.current_session_files = []
    st.session_state.show_history = False

def limpar_selecao():
    """Limpa apenas a seleção atual, mantendo o histórico"""
    st.session_state.selected_label = None
    st.session_state.selected_schedule_id = None
    
# ================== Sidebar ==================
with st.sidebar:
    st.header("Configuração")
    st.write("Defina suas credenciais (em .env ou ambiente):")
    st.code("NIBO_API_TOKEN=SEU_TOKEN_AQUI", language="bash")
    st.caption("Usa header ApiToken (ou parâmetro apitoken).")
    
    # Botões para controle de sessão
    st.markdown("---")
    st.subheader("Controle de Conciliação")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Nova Conciliação", use_container_width=True):
            nova_sessao()
            st.rerun()
    with col2:
        if st.button("Limpar Seleção", use_container_width=True):
            limpar_selecao()
            st.rerun()
    
    # Histórico de conciliações
    if st.toggle("Mostrar histórico", value=st.session_state.get("show_history", False)):
        st.session_state.show_history = True
        if "history" in st.session_state and st.session_state.history:
            st.markdown("### Conciliações anteriores")
            for idx, item in enumerate(st.session_state.history):
                with st.expander(f"{item['date']} - {item['schedule_label'][:20]}..."):
                    st.write(f"**Agendamento:** {item['schedule_label']}")
                    st.write(f"**Arquivos anexados:** {len(item['files'])}")
                    for file in item['files']:
                        st.write(f"- {file['name']}")
        else:
            st.info("Nenhum histórico de conciliação disponível")
    else:
        st.session_state.show_history = False

# ================== Estado ==================
if "uploaded_file_ids" not in st.session_state:
    st.session_state.uploaded_file_ids = []

if "last_results" not in st.session_state:
    st.session_state.last_results = []

if "pending_uploads" not in st.session_state:
    st.session_state.pending_uploads = []

if "selected_label" not in st.session_state:
    st.session_state.selected_label = None

if "selected_schedule_id" not in st.session_state:
    st.session_state.selected_schedule_id = None

if "current_session_files" not in st.session_state:
    st.session_state.current_session_files = []

if "history" not in st.session_state:
    st.session_state.history = []

if "show_history" not in st.session_state:
    st.session_state.show_history = False

# ================== Layout principal com tabs ==================
tab1, tab2 = st.tabs(["Busca e Conciliação", "Ajuda"])

with tab1:
    # Container para destacar sessão atual
    with st.container(border=True):
        if st.session_state.selected_schedule_id:
            st.success(f"Conciliação atual: {st.session_state.selected_label}")
            st.caption("Para iniciar uma nova conciliação, clique em 'Nova Conciliação' no menu lateral")
        else:
            st.info("Nenhum agendamento selecionado para conciliação. Busque e selecione um agendamento abaixo.")
    
    # Layout principal dividido em duas colunas
    col_busca, col_arquivos = st.columns([3, 2])
    
    # Coluna de busca e seleção de agendamentos
    with col_busca:
        st.subheader("1. Buscar e selecionar agendamento")
        
        col_kind, col_scope = st.columns(2)
        with col_kind:
            kind = st.radio("Tipo", options=("Pagamentos (debit)", "Recebimentos (credit)"), horizontal=True)
            kind_key = "debit" if kind.startswith("Pagamentos") else "credit"
            st.session_state.kind_key = kind_key
        with col_scope:
            opened_only = st.toggle("Listar apenas abertos", value=True, help="Desative para listar TODOS")

        # Filtros em linha
        col1, col2 = st.columns([3, 1])
        with col1:
            desc_contains = st.text_input("Buscar por descrição (número, texto, etc)", value="", placeholder="Ex: 3344, NF, pagamento")
        with col2:
            top = st.number_input("Quantidade", min_value=1, max_value=500, value=100, step=10)
        
        # Filtros avançados (colapsáveis)
        with st.expander("Filtros avançados"):
            col_d1, col_d2 = st.columns(2)
            with col_d1:
                d_start = st.date_input("Data inicial (dueDate ≥)", value=None, format="YYYY-MM-DD")
            with col_d2:
                d_end = st.date_input("Data final (dueDate ≤)", value=None, format="YYYY-MM-DD")
                
            col_min, col_max, col_order = st.columns(3)
            with col_min:
                min_val_str = st.text_input("Valor mínimo", value="")
            with col_max:
                max_val_str = st.text_input("Valor máximo", value="")
            with col_order:
                order = st.selectbox("Ordenação", options=["dueDate desc", "dueDate asc", "value desc", "value asc"])

        # Parse min_val and max_val
        try:
            min_val = float(min_val_str) if min_val_str.strip() else None
        except ValueError:
            min_val = None
        try:
            max_val = float(max_val_str) if max_val_str.strip() else None
        except ValueError:
            max_val = None

        # Botão de busca
        if st.button("Buscar", key="btn_search", use_container_width=True):
            with st.spinner("Buscando agendamentos..."):
                try:
                    # Constrói o filtro OData
                    stakeholder_free = ""
                    odatabuilder_extra = ""
                    
                    odata_from_ui = build_odata_filter(
                        d_start if isinstance(d_start, date) else None,
                        d_end if isinstance(d_end, date) else None,
                        stakeholder_free if stakeholder_free.strip() else None,
                        desc_contains if desc_contains.strip() else None,
                        min_val, max_val
                    )

                    final_filter = ""
                    if odata_from_ui and odatabuilder_extra:
                        final_filter = f"({odata_from_ui}) and ({odatabuilder_extra})"
                    elif odata_from_ui:
                        final_filter = odata_from_ui
                    elif odatabuilder_extra:
                        final_filter = odatabuilder_extra

                    # Busca os agendamentos
                    results = list_schedules(kind_key, opened_only, top=top, orderby=order, odata_filter=final_filter)
                    
                    # Filtra resultados com número específico na descrição (se for número)
                    if desc_contains.strip() and is_number(desc_contains.strip()):
                        results = [r for r in results if desc_contains.strip() in (r.get("description") or "")]
                    
                    st.session_state.last_results = results or []
                    
                    if not results:
                        st.info("Nenhum agendamento encontrado com esses critérios.")
                    else:
                        st.success(f"Encontrados {len(results)} agendamentos.")
                except Exception as e:
                    st.error(f"Erro na busca: {str(e)}")

        # Resultados da busca (agendamentos)
        if st.session_state.last_results:
            st.markdown("### Agendamentos encontrados")
            
            # Prepara os dados
            options = []
            id_map = {}
            for it in st.session_state.last_results:
                lbl = schedule_label(it)
                sid = it.get("id") or it.get("scheduleId") or it.get("Id") or it.get("ScheduleId")
                if sid:
                    options.append(lbl or sid)
                    id_map[lbl or sid] = sid
            
            # Exibe cada agendamento como card
            for idx, lbl in enumerate(options):
                sid = id_map[lbl]
                with st.container(border=True):
                    col1, col2 = st.columns([4, 1])
                    with col1:
                        st.markdown(f"**{lbl}**")
                    with col2:
                        # Botão para selecionar o agendamento
                        if st.button("Selecionar", key=f"card_{sid}"):
                            # Se estiver começando nova conciliação, limpa arquivos anteriores
                            if st.session_state.selected_schedule_id != sid:
                                st.session_state.current_session_files = []
                            st.session_state.selected_label = lbl
                            st.session_state.selected_schedule_id = sid
                            st.rerun()
                    
                    # Destaca o agendamento selecionado
                    if st.session_state.selected_schedule_id == sid:
                        st.success("✓ Selecionado para conciliação")
    
    # Coluna de arquivos e anexos
    with col_arquivos:
        st.subheader("2. Upload e anexação de arquivos")
        
        # Upload de arquivos - sempre visível
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
        
        # Mostra arquivos pendentes para upload
        if st.session_state.pending_uploads:
            st.markdown("### Arquivos pendentes para upload")
            
            for idx, up in enumerate(st.session_state.pending_uploads[:]):
                with st.container(border=True):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.write(f"{up.name} ({up.size/1024:.1f} KB)")
                    with col2:
                        # Botão de upload desabilitado se não houver agendamento selecionado
                        if not st.session_state.selected_schedule_id:
                            st.button("Upload", key=f"btn_upload_disabled_{idx}", disabled=True)
                            st.caption("Selecione um agendamento primeiro")
                        else:
                            if st.button("Upload", key=f"btn_upload_{idx}_{up.name}"):
                                with st.spinner(f"Enviando {up.name}..."):
                                    try:
                                        resp = upload_file_to_nibo(up.name, up.getvalue(), up.type)
                                        fid = extract_file_id(resp)
                                        if fid:
                                            # Adiciona o arquivo à sessão atual
                                            st.session_state.current_session_files.append({
                                                "name": up.name,
                                                "id": fid,
                                                "size": up.size,
                                                "uploaded_at": datetime.now().isoformat()
                                            })
                                            
                                            # Remove o arquivo da lista de pendentes
                                            st.session_state.pending_uploads.remove(up)
                                            st.success(f"Upload concluído: {up.name}")
                                            st.rerun()
                                    except Exception as e:
                                        st.error(f"Erro no upload de {up.name}: {e}")
        
        # Exibe mensagem se não tiver agendamento selecionado
        if not st.session_state.selected_schedule_id:
            st.warning("⚠️ Selecione um agendamento na coluna da esquerda para fazer upload e anexar arquivos")
        
        # Lista de arquivos da sessão atual
        if st.session_state.current_session_files:
            st.markdown("### Arquivos da conciliação atual")
            
            # Mostra cada arquivo com opção de anexar
            for idx, file_info in enumerate(st.session_state.current_session_files):
                with st.container(border=True):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.write(f"{file_info['name']} ({file_info['size']/1024:.1f} KB)")
                        if "attached" in file_info and file_info["attached"]:
                            st.success("✓ Anexado")
                    with col2:
                        # Botão de anexar (se não estiver anexado)
                        if not file_info.get("attached"):
                            if st.button("Anexar", key=f"attach_{idx}"):
                                with st.spinner("Anexando arquivo..."):
                                    try:
                                        ok, msg = attach_files(
                                            st.session_state.kind_key,
                                            st.session_state.selected_schedule_id,
                                            [file_info['id']]
                                        )
                                        
                                        if ok:
                                            # Marca o arquivo como anexado
                                            file_info["attached"] = True
                                            file_info["attached_at"] = datetime.now().isoformat()
                                            st.success(f"Arquivo anexado com sucesso!")
                                            st.rerun()
                                        else:
                                            st.error(msg)
                                    except Exception as e:
                                        st.error(f"Erro ao anexar: {e}")
            
            # Botão para concluir a conciliação
            if all(file.get("attached", False) for file in st.session_state.current_session_files):
                if st.button("✅ Concluir Conciliação", use_container_width=True):
                    # Adiciona ao histórico
                    st.session_state.history.append({
                        "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
                        "schedule_id": st.session_state.selected_schedule_id,
                        "schedule_label": st.session_state.selected_label,
                        "files": st.session_state.current_session_files.copy(),
                        "kind": st.session_state.kind_key
                    })
                    
                    # Inicia nova conciliação
                    nova_sessao()
                    st.success("Conciliação concluída com sucesso!")
                    st.rerun()

with tab2:
    st.subheader("Ajuda")
    st.markdown("""
    ### Como usar a Conciliação do Nibo

    Esta ferramenta permite fazer o upload de arquivos e anexá-los a agendamentos no Nibo, 
    mantendo cada conciliação como uma operação independente.

    #### Passo a passo:

    1. **Buscar agendamento**:
       - Use os filtros na coluna da esquerda para localizar o agendamento desejado
       - Você pode buscar por número específico na descrição, data ou valor
       - Selecione o agendamento clicando no botão "Selecionar"

    2. **Upload e anexação de arquivos**:
       - Faça o upload dos arquivos relacionados ao agendamento selecionado
       - Cada arquivo aparecerá na lista de "Arquivos da conciliação atual"
       - Anexe cada arquivo clicando no botão "Anexar"
       - Quando todos os arquivos estiverem anexados, clique em "Concluir Conciliação"

    3. **Iniciar nova conciliação**:
       - A qualquer momento, você pode clicar em "Nova Conciliação" na barra lateral
       - Isso limpará a seleção atual e os arquivos pendentes
       - O histórico de conciliações anteriores fica disponível no menu lateral

    #### Dicas:
    - Para buscar agendamentos com um número específico na descrição, digite-o no campo de busca
    - Você pode alternar entre ver pagamentos e recebimentos conforme necessário
    - O histórico permite verificar conciliações anteriores
    """)

# Rodapé
st.divider()
st.caption("Ferramenta de Conciliação Nibo • Para cada conciliação nova, use 'Nova Conciliação' no menu lateral")
