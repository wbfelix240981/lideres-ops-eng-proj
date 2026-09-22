#!/usr/bin/env python3
"""
Sincroniza os dados dos 6 líderes (Wagner, Bruno, Leonardo, João, Gustavo,
Rodney) e dos 2 clientes de projeto (SOPREMA, Pleion) com o ClickUp.
Roda via GitHub Actions (agendamento de hora em hora) ou manualmente.

O que é sincronizado automaticamente (fiel ao ClickUp):
- Quadros Kanban dos 6 líderes: todas as tarefas ativas da lista de cada um,
  mais as tarefas fechadas/concluídas (com data real de conclusão, usada no
  selo "Concluída há X dias").
- Metas de clientes (SOPREMA e Pleion): reconstruídas a partir da hierarquia
  real de subtarefas do ClickUp (Fase > Atividade), preservando o link direto
  "abrir no ClickUp" de cada atividade.

O que NÃO é feito automaticamente:
- Inclusão de um cliente novo (uma 3ª meta) — isso precisa ser pedido
  explicitamente, já que exige nome, tags e descrição.
- Mudança na lista de líderes ou nos IDs de lista mapeados abaixo.
"""
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.error

TOKEN = os.environ["CLICKUP_API_TOKEN"]
BASE = "https://api.clickup.com/api/v2"
DIR = os.path.dirname(os.path.abspath(__file__))
ERROR_LOG_PATH = os.path.join(DIR, "sync_errors.log")

# --- Mapeamento de listas do ClickUp ---------------------------------------
# Cada lista pode precisar de um valor diferente para o parâmetro
# "subtasks" da API do ClickUp — descobrimos isso na prática (ver
# automation/sync_errors.log, entradas "DIAGNOSTICO", e o histórico do
# repositório): a lista do João tem muitos itens que são tecnicamente
# subtarefas no ClickUp (os controles numerados tipo "14.1", "14.2"), então
# só aparecem por completo com subtasks=true. Já em outras listas (mais
# "flat", sem essa hierarquia) subtasks=true faz o ClickUp devolver tarefas
# duplicadas. Por isso o valor é configurado individualmente por lista, e
# não como uma opção global.
# A lista do João (901326962545) é a única que nunca voltou certa em
# nenhuma combinação testada: subtasks=false traz só 7 das 165 tarefas reais,
# e subtasks=true traz 283 (praticamente o dobro do certo). Não conseguimos
# entender a causa exata sem acesso direto para depurar a API ao vivo. Por
# isso ela fica com subtasks=false (mais parecido com as outras listas) e
# CONFIAMOS na trava de segurança abaixo (MIN_FRACTION_OF_PREVIOUS) para
# simplesmente não sobrescrever o arquivo sempre que isso acontecer — ou
# seja, o quadro do João não se atualiza sozinho até isso ser investigado
# com mais calma, mas também não perde dados.
LEADER_LISTS = {
    "wagner_tasks.json": {"list_id": "901326954601", "subtasks": False},
    "bruno_tasks.json": {"list_id": "901318773612", "subtasks": False},
    "leonardo_tasks.json": {"list_id": "901328009767", "subtasks": False},
    "joao_tasks.json": {"list_id": "901326962545", "subtasks": False},
    "gustavo_tasks.json": {"list_id": "901318774255", "subtasks": False},
    "rodney_tasks.json": {"list_id": "901323507381", "subtasks": False},
}

# Clientes de projeto (Soprema/Pleion) removidos da página de Projetos a
# pedido do usuário. Deixe este dicionário vazio para que wagner_metas.json
# permaneça sem cards de cliente. Para reativar algum, basta adicionar de
# volta a entrada correspondente aqui.
CLIENT_METAS = {}

STATUS_MAP = {
    "fechado": "shipped",
    "concluído": "shipped",
    "aberto": "backlog",
    "em andamento": "in progress",
    "revisando": "in review",
    "em revisão": "in review",
    "aguardando retorno": "in review",
    "bloqueada": "blocked",
    "impedimento": "blocked",
    "em planejamento": "in planning",
    "planejamento": "in planning",
    "em teste": "in test",
    "rejeitada": "backlog",
}


def log_error(contexto, exc):
    """Grava o erro num arquivo que é commitado junto com os dados, já que os
    logs brutos do GitHub Actions não são fáceis de consultar depois. Mantém
    só as últimas 50 entradas para o arquivo não crescer indefinidamente."""
    import traceback
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # traceback.format_exc() só funciona dentro de um "except" ativo — fora
    # disso (como numa mensagem de diagnóstico) ele retorna "NoneType: None"
    # e perde a mensagem de verdade. Por isso usamos str(exc) como conteúdo
    # principal, com o traceback (se existir) como informação extra.
    tb = traceback.format_exc()
    detalhe = str(exc) if tb.strip() == "NoneType: None" else tb
    entrada = f"[{ts}] {contexto}\n{detalhe}\n{'-'*70}\n"
    linhas_antigas = ""
    if os.path.exists(ERROR_LOG_PATH):
        with open(ERROR_LOG_PATH, encoding="utf-8") as f:
            linhas_antigas = f.read()
    blocos = (linhas_antigas + entrada).split('-'*70 + "\n")
    blocos = [b for b in blocos if b.strip()][-50:]
    with open(ERROR_LOG_PATH, "w", encoding="utf-8") as f:
        f.write(('-'*70 + "\n").join(blocos) + ('-'*70 + "\n" if blocos else ""))


def api_get(path):
    url = BASE + path
    req = urllib.request.Request(url, headers={"Authorization": TOKEN})
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(5)
    raise RuntimeError(f"Falha ao buscar {url}: {last_err}")


def api_get_all_tasks(list_id, include_closed=True, subtasks=False):
    """Busca TODAS as tarefas de uma lista, paginando corretamente.

    O parâmetro "subtasks" precisa ser configurado por lista (ver
    LEADER_LISTS) — diagnóstico real em automation/sync_errors.log mostrou
    que a lista do João só devolve todos os itens com subtasks=true (ela tem
    muitos itens que são tecnicamente subtarefas), enquanto outras listas
    (mais "flat") duplicam tarefas quando subtasks=true é usado nelas.
    """
    todas = []
    page = 0
    closed_param = "true" if include_closed else "false"
    subtasks_param = "true" if subtasks else "false"
    PAGE_SIZE = 100
    diag_pages = []
    while True:
        resp = api_get(f"/list/{list_id}/task?subtasks={subtasks_param}&include_closed={closed_param}&page={page}")
        pagina_tasks = resp.get("tasks", [])
        diag_pages.append({
            "page": page,
            "qtd_retornada": len(pagina_tasks),
            "last_page_field": resp.get("last_page", "AUSENTE"),
        })
        todas.extend(pagina_tasks)
        if len(pagina_tasks) < PAGE_SIZE:
            break
        page += 1
        if page > 20:  # trava de segurança contra loop infinito
            break

    # Diagnóstico: detecta duplicatas (mesma tarefa devolvida mais de uma vez
    # dentro da mesma busca) para investigar o comportamento real da API.
    ids = [t["id"] for t in todas]
    duplicados = len(ids) - len(set(ids))
    diag_msg = (
        f"[DIAGNOSTICO] list_id={list_id} subtasks={subtasks_param} include_closed={closed_param} | "
        f"paginas={diag_pages} | total_bruto={len(todas)} | ids_unicos={len(set(ids))} | duplicados={duplicados}"
    )
    print("  " + diag_msg)
    log_error(f"diagnostico_paginacao({list_id})", Exception(diag_msg))

    if duplicados:
        # Remove duplicatas mantendo a primeira ocorrência de cada id.
        vistos = set()
        sem_dup = []
        for t in todas:
            if t["id"] not in vistos:
                vistos.add(t["id"])
                sem_dup.append(t)
        todas = sem_dup

    return todas


def assignee_names(task):
    return [a["username"] for a in task.get("assignees", [])]


def to_ms(due_date):
    return int(due_date) if due_date else None


def map_status(raw_status):
    raw = (raw_status or "").strip().lower()
    return STATUS_MAP.get(raw, "backlog")


def raw_status_label(task):
    return (task.get("status") or {}).get("status", "aberto").strip().lower()


# --- Quadros Kanban dos 6 líderes -------------------------------------------

# Tarefas fechadas há mais tempo que isso não aparecem mais no quadro —
# evita poluir o board com anos de histórico irrelevante (algumas listas,
# como a do Bruno, têm 200+ tarefas fechadas antigas). Ajuste livremente.
CLOSED_LOOKBACK_MS = 180 * 24 * 60 * 60 * 1000  # 180 dias


# Se a busca trouxer muito menos ou muito mais itens do que já existia,
# provavelmente algo deu errado (bug de paginação/subtarefas, erro da API,
# etc.) e o arquivo NÃO é sobrescrito — fica com os dados anteriores. Isso
# evita repetir os dois incidentes reais que já aconteceram aqui: o quadro
# do João caindo de 165 para 7 tarefas, e depois inchando para 283.
MIN_FRACTION_OF_PREVIOUS = 0.5
MAX_MULTIPLE_OF_PREVIOUS = 1.5


def sync_leader_board(fname, list_id, subtasks=False):
    tasks = api_get_all_tasks(list_id, include_closed=True, subtasks=subtasks)
    agora = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    out = []
    for t in tasks:
        raw_status = raw_status_label(t)
        date_closed = t.get("date_closed")
        # Fechada há mais de 180 dias: não entra no quadro (ruído histórico).
        if date_closed and (agora - int(date_closed)) > CLOSED_LOOKBACK_MS:
            continue
        entry = {
            "id": t["id"],
            "name": t["name"],
            "status": raw_status,
            "assignees": assignee_names(t),
        }
        due = to_ms(t.get("due_date"))
        if due:
            entry["due"] = due
        entry["url"] = f"https://app.clickup.com/t/{t['id']}"
        if date_closed:
            entry["doneAt"] = int(date_closed)
        out.append(entry)

    path = os.path.join(DIR, fname)

    previous = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                previous = json.load(f)
        except Exception:
            previous = []

    if previous:
        muito_pouco = len(out) < len(previous) * MIN_FRACTION_OF_PREVIOUS
        muito_mais = len(out) > len(previous) * MAX_MULTIPLE_OF_PREVIOUS
        if muito_pouco or muito_mais:
            motivo = "menos" if muito_pouco else "mais"
            log_error(
                f"sync_leader_board({fname})",
                Exception(
                    f"Fetch retornou {len(out)} tarefas, {motivo} do que era "
                    f"esperado a partir das {len(previous)} que já existiam. "
                    f"Mantendo dados anteriores intactos para não arriscar "
                    f"perder ou duplicar informação real."
                ),
            )
            print(f"  AVISO {fname}: fetch trouxe {len(out)} vs {len(previous)} esperadas — mantendo dados antigos.")
            return previous

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"  {fname}: {len(out)} tarefas")
    return out


# --- Metas de clientes (fases reais do ClickUp) -----------------------------

def build_ativ(task):
    return {
        "n": task["name"],
        "status": map_status(raw_status_label(task)),
        "due": to_ms(task.get("due_date")),
        **({"doneAt": int(task["date_closed"])} if task.get("date_closed") else {}),
        **({"assignee": assignee_names(task)[0]} if assignee_names(task) else {}),
        "id": task["id"],
        "url": f"https://app.clickup.com/t/{task['id']}",
    }


def build_fase(fase_task, subtasks):
    ativ = [build_ativ(s) for s in subtasks]
    return {
        "n": fase_task["name"],
        "status": map_status(raw_status_label(fase_task)),
        "assignee": (assignee_names(fase_task) or ["—"])[0],
        "due": to_ms(fase_task.get("due_date")),
        "id": fase_task["id"],
        "ativ": ativ,
    }


def sync_client_meta(meta_id, cfg):
    """Reconstrói uma meta de cliente a partir da hierarquia real do ClickUp.
    Tarefas nomeadas 'Fase N - ...' viram cabeçalhos de fase; as demais
    tarefas da lista são agrupadas na fase mais recente que apareceu antes
    delas na ordenação da lista (aproximação razoável quando o ClickUp não
    usa hierarquia de subtarefa de verdade nessa lista)."""
    tasks = api_get_all_tasks(cfg["list_id"], include_closed=True)
    # Ordena por data de criação (mais antigo primeiro) para reconstruir a
    # ordem lógica de criação das fases e atividades.
    tasks.sort(key=lambda t: int(t.get("date_created") or 0))

    fase_tasks = [t for t in tasks if t["name"].strip().lower().startswith("fase ")]
    outras_tasks = [t for t in tasks if not t["name"].strip().lower().startswith("fase ")]

    if not fase_tasks:
        log_error(f"sync_client_meta({meta_id})", Exception("Nenhuma tarefa 'Fase N' encontrada na lista"))
        return None

    # Agrupa cada tarefa "normal" na fase mais próxima cronologicamente.
    fase_tasks_sorted = sorted(fase_tasks, key=lambda t: int(t.get("date_created") or 0))
    fases = []
    for i, ft in enumerate(fase_tasks_sorted):
        fases.append({"task": ft, "subtasks": []})

    for ot in outras_tasks:
        ot_created = int(ot.get("date_created") or 0)
        melhor_idx = 0
        for i, ft in enumerate(fase_tasks_sorted):
            if int(ft.get("date_created") or 0) <= ot_created:
                melhor_idx = i
        fases[melhor_idx]["subtasks"].append(ot)

    fases_out = [build_fase(f["task"], f["subtasks"]) for f in fases]

    meta = {
        "num": cfg["num"],
        "id": meta_id,
        "status": "in progress",
        "due": None,
        "name": cfg["name"],
        "short": cfg["short"],
        "assignee": "Wagner Felix",
        "tags": ["cliente", "migração"],
        "detail": cfg["detail"],
        "sourceUrl": cfg["source_url"],
        "fases": fases_out,
    }
    return meta


def sync_wagner_metas():
    metas = []
    for meta_id, cfg in CLIENT_METAS.items():
        try:
            meta = sync_client_meta(meta_id, cfg)
            if meta:
                metas.append(meta)
        except Exception as e:
            log_error(f"sync_client_meta({meta_id})", e)
    path = os.path.join(DIR, "wagner_metas.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(metas, f, ensure_ascii=False, indent=1)
    print(f"  wagner_metas.json: {len(metas)} metas de cliente")


def main():
    print("Sincronizando quadros dos líderes...")
    for fname, cfg in LEADER_LISTS.items():
        try:
            sync_leader_board(fname, cfg["list_id"], subtasks=cfg.get("subtasks", False))
        except Exception as e:
            log_error(f"sync_leader_board({fname})", e)
            print(f"  ERRO em {fname}, mantendo dados anteriores.")

    print("Sincronizando metas de clientes...")
    try:
        sync_wagner_metas()
    except Exception as e:
        log_error("sync_wagner_metas", e)
        print("  ERRO ao sincronizar metas de clientes, mantendo dados anteriores.")

    last_sync = {"last_sync_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with open(os.path.join(DIR, "last_sync.json"), "w", encoding="utf-8") as f:
        json.dump(last_sync, f)
    print("Sincronização concluída:", last_sync["last_sync_utc"])


if __name__ == "__main__":
    main()
