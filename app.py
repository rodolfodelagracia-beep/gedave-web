# app.py
import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from io import BytesIO
import random

import pandas as pd
import streamlit as st

# PDF (reportlab)
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet


# =========================
# CONFIG
# =========================
st.set_page_config(page_title="Gestão de Rebanho GEDAVE", layout="wide", page_icon="🐄")

APP_TITLE = "Gestão de Rebanho - GEDAVE SP"
DB_FILE = os.getenv("GEDAVE_DB_FILE", "fazenda.db")
SCHEMA_VERSION = 2

STATUS_OPCOES = ["Ativo", "Vendido", "Morto", "Roubado"]
SEXO_OPCOES = ["M", "F"]
SEXO_MAP = {"M": "Macho", "F": "Fêmea"}

FAIXAS_ORDEM = [
    "00 a 02 meses",
    "03 a 08 meses",
    "09 a 12 meses",
    "13 a 24 meses",
    "25 a 36 meses",
    "Acima de 36 meses",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================
# DB HELPERS
# =========================
@contextmanager
def db():
    """
    Conexão SQLite com WAL e FK, commit/rollback automáticos.
    """
    conn = sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception("Erro em operação de banco")
        raise
    finally:
        conn.close()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cur.fetchone() is not None


def get_schema_version(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "meta"):
        return 0
    row = conn.execute("SELECT schema_version FROM meta LIMIT 1").fetchone()
    return int(row["schema_version"]) if row else 0


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("DELETE FROM meta")
    conn.execute("INSERT INTO meta(schema_version, updated_at) VALUES (?, datetime('now'))", (version,))


def init_db():
    """
    Cria estrutura base se não existir e aplica migrações.
    """
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                schema_version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        current_version = get_schema_version(conn)

        # Se não existe tabela principal, cria já no schema atual
        if not table_exists(conn, "animais"):
            _create_schema_v2(conn)
            set_schema_version(conn, SCHEMA_VERSION)
            logging.info("DB criado com schema v2.")
            return

        # Se existe, migra se necessário
        if current_version < 1:
            # Caso antigo sem meta: assume v1 "legacy" e migra para v2
            _migrate_legacy_to_v2(conn)
            set_schema_version(conn, SCHEMA_VERSION)
            logging.info("DB legacy migrado para schema v2.")
            return

        if current_version == 1:
            _migrate_v1_to_v2(conn)
            set_schema_version(conn, SCHEMA_VERSION)
            logging.info("DB migrado de v1 para v2.")
            return

        # v2 ok
        if current_version >= SCHEMA_VERSION:
            return


def _create_schema_v2(conn: sqlite3.Connection) -> None:
    """
    Schema v2: animais + movimentacoes + índices + constraints.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS animais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brinco TEXT UNIQUE NOT NULL,
            data_nascimento TEXT NOT NULL,         -- ISO YYYY-MM-DD
            sexo TEXT NOT NULL CHECK (sexo IN ('M','F')),
            data_entrada TEXT NOT NULL,            -- ISO YYYY-MM-DD
            status TEXT NOT NULL DEFAULT 'Ativo'
                CHECK (status IN ('Ativo','Vendido','Morto','Roubado')),
            vacinada_brucelose INTEGER NOT NULL DEFAULT 0
                CHECK (vacinada_brucelose IN (0,1)),
            data_vacina_brucelose TEXT,            -- ISO YYYY-MM-DD (opcional)
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS movimentacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            animal_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,                    -- ENTRADA, VENDA, MORTE, ROUBO, REATIVACAO, VACINA_BRUCELOSE, OBS
            data_evento TEXT NOT NULL,             -- ISO YYYY-MM-DD
            observacao TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (animal_id) REFERENCES animais(id) ON DELETE CASCADE
        )
    """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_animais_status ON animais(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_animais_sexo_nasc ON animais(sexo, data_nascimento)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mov_animal_data ON movimentacoes(animal_id, data_evento)")


def _migrate_legacy_to_v2(conn: sqlite3.Connection) -> None:
    """
    Migração de um DB antigo (seu schema original) para v2.
    A estratégia é reconstrução segura: cria tabela nova, copia, recria índices.
    """
    # Detecta colunas do legacy
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(animais)").fetchall()]
    has_vac = "vacinada_brucelose" in cols

    # Cria tabelas novas (temporárias)
    conn.execute("ALTER TABLE animais RENAME TO animais_legacy")

    _create_schema_v2(conn)

    # Copia dados com normalização
    legacy_rows = conn.execute("SELECT * FROM animais_legacy").fetchall()

    for r in legacy_rows:
        brinco = (r["brinco"] or "").strip().upper()
        if not brinco:
            continue

        data_nasc = _safe_iso_date(r["data_nascimento"])
        data_ent = _safe_iso_date(r["data_entrada"]) or data_nasc

        sexo = (r["sexo"] or "F").strip().upper()
        if sexo not in SEXO_OPCOES:
            sexo = "F"

        status = (r["status"] or "Ativo").strip().title()
        if status not in STATUS_OPCOES:
            status = "Ativo"

        vac = 0
        if has_vac:
            vac = 1 if _as_bool01(r["vacinada_brucelose"]) else 0
        if sexo == "M":
            vac = 0

        conn.execute("""
            INSERT OR IGNORE INTO animais
            (brinco, data_nascimento, sexo, data_entrada, status, vacinada_brucelose, data_vacina_brucelose)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
        """, (brinco, data_nasc, sexo, data_ent, status, vac))

    # Histórico mínimo: cria movimentação ENTRADA para todos os animais ativos (e também para os demais)
    new_animals = conn.execute("SELECT id, data_entrada FROM animais").fetchall()
    for a in new_animals:
        conn.execute("""
            INSERT INTO movimentacoes(animal_id, tipo, data_evento, observacao)
            VALUES (?, 'ENTRADA', ?, 'Migrado do banco legacy')
        """, (a["id"], a["data_entrada"]))

    # Remove legacy
    conn.execute("DROP TABLE animais_legacy")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    Caso você já tenha uma versão v1 intermediária, migra para v2.
    Aqui, garantimos tabela movimentacoes e coluna data_vacina_brucelose.
    """
    _create_schema_v2(conn)

    # Se v1 já tinha animais, tenta adicionar coluna se estiver faltando
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(animais)").fetchall()]
    if "data_vacina_brucelose" not in cols:
        conn.execute("ALTER TABLE animais ADD COLUMN data_vacina_brucelose TEXT")

    if "updated_at" not in cols:
        conn.execute("ALTER TABLE animais ADD COLUMN updated_at TEXT NOT NULL DEFAULT (datetime('now'))")

    if "created_at" not in cols:
        conn.execute("ALTER TABLE animais ADD COLUMN created_at TEXT NOT NULL DEFAULT (datetime('now'))")

    # movimentacoes já criada no _create_schema_v2


def reset_db():
    """
    Apaga o arquivo do DB com segurança.
    """
    if os.path.exists(DB_FILE):
        try:
            os.remove(DB_FILE)
        except Exception:
            logging.exception("Falha ao remover DB")


# =========================
# UTIL
# =========================
def _safe_iso_date(value) -> str | None:
    """
    Converte várias entradas para ISO YYYY-MM-DD (string).
    Retorna None se não conseguir.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # tenta formatos comuns
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                pass
    return None


def _as_bool01(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return int(v) == 1
    if isinstance(v, str):
        s = v.strip().lower()
        return s in ("1", "true", "t", "yes", "y", "sim")
    return False


def calcular_idade_meses(data_nasc: date, data_ref: date) -> int:
    """
    Idade em meses completos.
    """
    # diferença base em meses
    delta = (data_ref.year * 12 + data_ref.month) - (data_nasc.year * 12 + data_nasc.month)
    # ajuste pelo dia
    if data_ref.day < data_nasc.day:
        delta -= 1
    return max(0, int(delta))


def classificar_faixa(meses: int) -> str:
    if 0 <= meses < 3:
        return "00 a 02 meses"
    if 3 <= meses < 9:
        return "03 a 08 meses"
    if 9 <= meses < 13:
        return "09 a 12 meses"
    if 13 <= meses < 25:
        return "13 a 24 meses"
    if 25 <= meses < 37:
        return "25 a 36 meses"
    if meses >= 37:
        return "Acima de 36 meses"
    return "Erro"


def normalize_brinco(brinco: str) -> str:
    return (brinco or "").strip().upper()


def invalidate_cache():
    st.session_state["cache_buster"] = st.session_state.get("cache_buster", 0) + 1


# =========================
# DATA ACCESS (com cache)
# =========================
@st.cache_data(show_spinner=False)
def load_animais(_cache_buster: int) -> pd.DataFrame:
    with db() as conn:
        df = pd.read_sql_query("SELECT * FROM animais", conn)
    if not df.empty:
        df["vacinada_brucelose"] = df["vacinada_brucelose"].astype(int)
    return df


@st.cache_data(show_spinner=False)
def load_movimentacoes(_cache_buster: int, animal_id: int) -> pd.DataFrame:
    with db() as conn:
        df = pd.read_sql_query(
            """
            SELECT m.*, a.brinco
            FROM movimentacoes m
            JOIN animais a ON a.id = m.animal_id
            WHERE m.animal_id = ?
            ORDER BY date(m.data_evento) DESC, m.id DESC
            """,
            conn,
            params=(animal_id,)
        )
    return df


def insert_animal(brinco: str, data_nasc: date, sexo: str, data_entrada: date, vacinada_brucelose: bool):
    brinco = normalize_brinco(brinco)
    if not brinco:
        raise ValueError("Brinco inválido.")

    if sexo not in SEXO_OPCOES:
        raise ValueError("Sexo inválido.")

    if data_nasc > data_entrada:
        raise ValueError("Data de nascimento não pode ser maior que a data de entrada.")

    vac = 1 if (sexo == "F" and vacinada_brucelose) else 0

    with db() as conn:
        conn.execute("""
            INSERT INTO animais (brinco, data_nascimento, sexo, data_entrada, status, vacinada_brucelose, data_vacina_brucelose)
            VALUES (?, ?, ?, ?, 'Ativo', ?, NULL)
        """, (brinco, data_nasc.isoformat(), sexo, data_entrada.isoformat(), vac))

        animal_id = conn.execute("SELECT id FROM animais WHERE brinco = ?", (brinco,)).fetchone()["id"]

        conn.execute("""
            INSERT INTO movimentacoes (animal_id, tipo, data_evento, observacao)
            VALUES (?, 'ENTRADA', ?, 'Cadastro do animal')
        """, (animal_id, data_entrada.isoformat()))

    invalidate_cache()


def hard_delete_animal(brinco: str):
    brinco = normalize_brinco(brinco)
    with db() as conn:
        cur = conn.execute("DELETE FROM animais WHERE brinco = ?", (brinco,))
        deleted = cur.rowcount
    if deleted:
        invalidate_cache()
    return deleted


def update_animais_from_editor(df_base: pd.DataFrame, df_edited: pd.DataFrame):
    """
    Atualiza apenas o que mudou (status/vacina/data_vacina).
    """
    if df_base.empty or df_edited.empty:
        return 0

    base = df_base.set_index("id")[["status", "vacinada_brucelose", "data_vacina_brucelose", "sexo"]].copy()
    novo = df_edited.set_index("id")[["status", "vacinada_brucelose", "data_vacina_brucelose", "sexo"]].copy()

    # normaliza
    novo["vacinada_brucelose"] = novo["vacinada_brucelose"].apply(lambda x: 1 if _as_bool01(x) else 0)
    novo["status"] = novo["status"].astype(str).apply(lambda x: x.strip().title())
    novo["data_vacina_brucelose"] = novo["data_vacina_brucelose"].apply(_safe_iso_date)

    # regra: macho não pode ficar vacinado
    novo.loc[novo["sexo"] == "M", "vacinada_brucelose"] = 0
    novo.loc[novo["sexo"] == "M", "data_vacina_brucelose"] = None

    # detecta diff
    changed = novo[
        (novo["status"] != base["status"]) |
        (novo["vacinada_brucelose"] != base["vacinada_brucelose"]) |
        (novo["data_vacina_brucelose"].fillna("") != base["data_vacina_brucelose"].fillna(""))
    ].copy()

    if changed.empty:
        return 0

    params = []
    for idx, r in changed.iterrows():
        status = r["status"] if r["status"] in STATUS_OPCOES else "Ativo"
        vac = int(r["vacinada_brucelose"])
        dtvac = r["data_vacina_brucelose"]
        params.append((status, vac, dtvac, idx))

    with db() as conn:
        conn.executemany("""
            UPDATE animais
            SET status = ?, vacinada_brucelose = ?, data_vacina_brucelose = ?, updated_at = datetime('now')
            WHERE id = ?
        """, params)

    invalidate_cache()
    return len(params)


def registrar_movimentacao(animal_id: int, tipo: str, data_evento: date, observacao: str | None):
    tipo = tipo.strip().upper()

    tipo_to_status = {
        "VENDA": "Vendido",
        "MORTE": "Morto",
        "ROUBO": "Roubado",
        "REATIVACAO": "Ativo",
    }

    with db() as conn:
        conn.execute("""
            INSERT INTO movimentacoes (animal_id, tipo, data_evento, observacao)
            VALUES (?, ?, ?, ?)
        """, (animal_id, tipo, data_evento.isoformat(), (observacao or "").strip() or None))

        if tipo in tipo_to_status:
            conn.execute("""
                UPDATE animais
                SET status = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (tipo_to_status[tipo], animal_id))

    invalidate_cache()


def registrar_vacina_brucelose(animal_id: int, data_vacina: date, observacao: str | None = None):
    with db() as conn:
        animal = conn.execute("SELECT sexo FROM animais WHERE id = ?", (animal_id,)).fetchone()
        if not animal:
            raise ValueError("Animal não encontrado.")
        if animal["sexo"] != "F":
            raise ValueError("Brucelose: vacinação aplicável apenas a fêmeas.")

        conn.execute("""
            UPDATE animais
            SET vacinada_brucelose = 1, data_vacina_brucelose = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (data_vacina.isoformat(), animal_id))

        conn.execute("""
            INSERT INTO movimentacoes (animal_id, tipo, data_evento, observacao)
            VALUES (?, 'VACINA_BRUCELOSE', ?, ?)
        """, (animal_id, data_vacina.isoformat(), (observacao or "").strip() or "Vacinação de brucelose registrada"))

    invalidate_cache()


# =========================
# SEED
# =========================
def seed_db_if_empty():
    """
    Popula com dados fictícios apenas se o banco estiver vazio.
    """
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM animais").fetchone()["c"]
        if count and int(count) > 0:
            return

    hoje = date.today()
    offsets = [30, 120, 300, 500, 900, 1200]  # faixas diversas
    animais_fake = []

    for i, dias in enumerate(offsets):
        nasc = hoje - timedelta(days=dias)
        for k in range(3):
            sexo = random.choice(["M", "F"])

            # força 1 fêmea 4 meses não vacinada
            if dias == 120 and k == 0:
                sexo = "F"
                vacinada = False
            else:
                vacinada = random.choice([True, False]) if sexo == "F" else False

            brinco = f"TESTE-{i}-{k}-{random.randint(100,999)}"
            animais_fake.append((brinco, nasc, sexo, hoje, vacinada))

    with db() as conn:
        for brinco, nasc, sexo, entrada, vac in animais_fake:
            br = normalize_brinco(brinco)
            vac01 = 1 if (sexo == "F" and vac) else 0
            conn.execute("""
                INSERT OR IGNORE INTO animais
                (brinco, data_nascimento, sexo, data_entrada, status, vacinada_brucelose, data_vacina_brucelose)
                VALUES (?, ?, ?, ?, 'Ativo', ?, NULL)
            """, (br, nasc.isoformat(), sexo, entrada.isoformat(), vac01))

            row = conn.execute("SELECT id FROM animais WHERE brinco = ?", (br,)).fetchone()
            if row:
                conn.execute("""
                    INSERT INTO movimentacoes (animal_id, tipo, data_evento, observacao)
                    VALUES (?, 'ENTRADA', ?, 'Seed de testes')
                """, (row["id"], entrada.isoformat()))

    invalidate_cache()
    st.toast("Banco populado com dados de teste (seed).", icon="✅")


# =========================
# EXPORT (CSV / PDF)
# =========================
def build_declaracao_frames(df_ativos: pd.DataFrame, data_ref: date):
    """
    Retorna pivot (saldo) e frames auxiliares para brucelose.
    """
    if df_ativos.empty:
        pivot = pd.DataFrame()
        femeas_bruc = pd.DataFrame()
        nao_vac = pd.DataFrame()
        return pivot, femeas_bruc, nao_vac, df_ativos

    df = df_ativos.copy()
    df["dt_nasc"] = pd.to_datetime(df["data_nascimento"]).dt.date
    df["meses_idade"] = df["dt_nasc"].apply(lambda d: calcular_idade_meses(d, data_ref))
    df["Faixa Etária"] = df["meses_idade"].apply(classificar_faixa)
    df["Sexo_Label"] = df["sexo"].map(SEXO_MAP)
    df["vacinada_brucelose"] = df["vacinada_brucelose"].astype(int)

    pivot = pd.crosstab(index=df["Sexo_Label"], columns=df["Faixa Etária"])
    pivot = pivot.reindex(columns=FAIXAS_ORDEM, fill_value=0)

    femeas_bruc = df[(df["sexo"] == "F") & (df["meses_idade"] >= 3) & (df["meses_idade"] < 9)].copy()
    nao_vac = femeas_bruc[femeas_bruc["vacinada_brucelose"] == 0].copy()

    return pivot, femeas_bruc, nao_vac, df


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=True).encode("utf-8-sig")


def make_declaracao_pdf(pivot: pd.DataFrame, nao_vac: pd.DataFrame, total_ativos: int, data_ref: date) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.5*cm,
        rightMargin=1.5*cm,
        topMargin=1.5*cm,
        bottomMargin=1.5*cm,
        title="Declaração de Rebanho - GEDAVE"
    )

    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("Declaração de Rebanho - GEDAVE (SP)", styles["Title"]))
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"Data de referência: <b>{data_ref.strftime('%d/%m/%Y')}</b>", styles["Normal"]))
    story.append(Paragraph(f"Total de animais ativos: <b>{total_ativos}</b>", styles["Normal"]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("1) Saldo de Rebanho (por faixa etária e sexo)", styles["Heading2"]))
    story.append(Spacer(1, 6))

    if pivot.empty:
        story.append(Paragraph("Nenhum animal ativo encontrado.", styles["Normal"]))
    else:
        table_data = [["Sexo"] + list(pivot.columns)]
        for idx, row in pivot.iterrows():
            table_data.append([idx] + [int(v) for v in row.values])

        t = Table(table_data, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ]))
        story.append(t)

    story.append(Spacer(1, 14))
    story.append(Paragraph("2) Controle de Brucelose (Fêmeas 03 a 08 meses)", styles["Heading2"]))
    story.append(Spacer(1, 6))

    if nao_vac.empty:
        story.append(Paragraph("Não há fêmeas 3–8 meses não vacinadas na base ativa.", styles["Normal"]))
    else:
        story.append(Paragraph(
            f"Atenção: <b>{len(nao_vac)}</b> fêmea(s) 3–8 meses sem vacinação registrada.",
            styles["Normal"]
        ))
        story.append(Spacer(1, 6))

        cols = ["brinco", "meses_idade", "data_vacina_brucelose"]
        nv = nao_vac.copy()
        nv["data_vacina_brucelose"] = nv["data_vacina_brucelose"].fillna("")
        table_data = [["Brinco", "Meses", "Data Vacina"]]
        for _, r in nv[cols].iterrows():
            table_data.append([str(r["brinco"]), int(r["meses_idade"]), str(r["data_vacina_brucelose"])])

        t2 = Table(table_data, hAlign="LEFT")
        t2.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ]))
        story.append(t2)

    doc.build(story)
    return buf.getvalue()


# =========================
# STARTUP
# =========================
init_db()
seed_db_if_empty()

if "cache_buster" not in st.session_state:
    st.session_state["cache_buster"] = 1


# =========================
# UI
# =========================
st.title(f"🚜 {APP_TITLE}")

with st.sidebar:
    st.header("Configurações")
    data_referencia = st.date_input("Data de referência (cálculo de idade)", value=date.today())
    st.info(f"Idades calculadas com base em: **{data_referencia.strftime('%d/%m/%Y')}**")

    st.divider()
    st.subheader("Banco de dados")

    if st.button("Resetar banco (apagar arquivo)", type="secondary"):
        st.session_state["confirm_reset"] = True

    if st.session_state.get("confirm_reset"):
        st.warning("Atenção: isso apaga o banco inteiro. Esta ação é irreversível.")
        confirm_text = st.text_input("Digite APAGAR para confirmar", key="reset_confirm_text")
        if st.button("Confirmar reset definitivo", type="primary"):
            if confirm_text.strip().upper() == "APAGAR":
                reset_db()
                st.success("Banco removido. Recarregando…")
                st.session_state["confirm_reset"] = False
                st.rerun()
            else:
                st.error("Confirmação incorreta. Digite APAGAR.")


tab1, tab2, tab3 = st.tabs(["📝 Cadastro e Movimentação", "📊 Painel GEDAVE", "🧾 Histórico / Auditoria"])


# =========================
# TAB 1
# =========================
with tab1:
    colA, colB = st.columns([1.1, 2.0])

    # ------- Cadastro + ações -------
    with colA:
        st.subheader("Novo animal")

        with st.form("form_novo_animal", clear_on_submit=True):
            brinco_input = st.text_input("Brinco (ID)", placeholder="Ex.: 12345 ou BR-001")
            nasc_input = st.date_input("Data de nascimento", value=date.today() - timedelta(days=30))
            sexo_input = st.radio("Sexo", SEXO_OPCOES, horizontal=True)
            entrada_input = st.date_input("Data de entrada", value=date.today())

            vacinada_input = st.checkbox(
                "Vacinada Brucelose?",
                value=False,
                disabled=(sexo_input == "M"),
                help="Aplicável apenas a fêmeas."
            )

            submitted = st.form_submit_button("Salvar animal", type="primary")

            if submitted:
                try:
                    insert_animal(
                        brinco=brinco_input,
                        data_nasc=nasc_input,
                        sexo=sexo_input,
                        data_entrada=entrada_input,
                        vacinada_brucelose=vacinada_input,
                    )
                    st.success(f"Animal {normalize_brinco(brinco_input)} salvo com sucesso.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Brinco já existe. Use um brinco único.")
                except Exception as e:
                    st.error(f"Erro ao salvar: {e}")

    # ------- Lista rápida (Feedback visual) -------
    with colB:
        st.write("### Últimas movimentações")
        # Carrega apenas para visualização rápida
        try:
            with db() as conn:
                ultimos = pd.read_sql_query("""
                    SELECT a.brinco, m.tipo, m.data_evento 
                    FROM movimentacoes m
                    JOIN animais a ON a.id = m.animal_id
                    ORDER BY m.id DESC LIMIT 5
                """, conn)
            if not ultimos.empty:
                st.dataframe(ultimos, use_container_width=True, hide_index=True)
            else:
                st.info("Nenhuma movimentação registrada ainda.")
        except Exception:
            st.error("Erro ao ler movimentações recentes.")

# =========================
# TAB 2: PAINEL E RELATÓRIOS
# =========================
with tab2:
    st.header("Gerenciamento do Rebanho")
    
    # Carrega dados
    df_animais = load_animais(st.session_state["cache_buster"])
    
    if df_animais.empty:
        st.warning("Nenhum animal cadastrado.")
    else:
        # Filtros rápidos
        col_f1, col_f2, col_f3 = st.columns(3)
        filtro_status = col_f1.multiselect("Filtrar Status", STATUS_OPCOES, default=["Ativo"])
        filtro_brinco = col_f2.text_input("Buscar Brinco")
        
        # Aplica filtros
        mask = df_animais["status"].isin(filtro_status)
        if filtro_brinco:
            mask = mask & df_animais["brinco"].astype(str).str.contains(filtro_brinco.upper())
        
        df_view = df_animais[mask].copy()
        
        # Estatísticas Rápidas
        total = len(df_view)
        machos = len(df_view[df_view["sexo"] == "M"])
        femeas = len(df_view[df_view["sexo"] == "F"])
        
        k1, k2, k3 = st.columns(3)
        k1.metric("Total Listado", total)
        k2.metric("Machos", machos)
        k3.metric("Fêmeas", femeas)
        
        st.divider()
        
        # --- EDITOR DE DADOS (Edição em massa) ---
        st.subheader("Edição Rápida (Status / Vacina)")
        st.caption("Altere 'Status' ou 'Vacinada' diretamente na tabela abaixo.")
        
        # Configura colunas para o st.data_editor
        edited_df = st.data_editor(
            df_view,
            column_config={
                "id": None, # Esconde ID
                "brinco": st.column_config.TextColumn("Brinco", disabled=True),
                "data_nascimento": st.column_config.DateColumn("Nascimento", disabled=True, format="DD/MM/YYYY"),
                "sexo": st.column_config.TextColumn("Sexo", disabled=True),
                "data_entrada": st.column_config.DateColumn("Entrada", disabled=True, format="DD/MM/YYYY"),
                "status": st.column_config.SelectboxColumn("Status", options=STATUS_OPCOES, required=True),
                "vacinada_brucelose": st.column_config.CheckboxColumn("Vac. Brucelose", default=False),
                "data_vacina_brucelose": st.column_config.DateColumn("Data Vacina", format="DD/MM/YYYY"),
                "created_at": None,
                "updated_at": None
            },
            hide_index=True,
            use_container_width=True,
            key="editor_animais"
        )
        
        # Botão para salvar edições
        if st.button("Salvar Alterações da Tabela"):
            changes = update_animais_from_editor(df_view, edited_df)
            if changes > 0:
                st.success(f"{changes} animais atualizados com sucesso!")
                st.rerun()
            else:
                st.info("Nenhuma alteração detectada para salvar.")

        st.divider()

        # --- GERAÇÃO DE PDF (GEDAVE) ---
        st.subheader("📄 Declaração de Rebanho")
        
        # Prepara dados para o PDF (apenas Ativos para o saldo oficial)
        df_ativos_pdf = df_animais[df_animais["status"] == "Ativo"].copy()
        pivot_table, femeas_pendentes, nao_vac, _ = build_declaracao_frames(df_ativos_pdf, data_referencia)
        
        col_pdf, col_csv = st.columns(2)
        
        with col_pdf:
            if st.button("Gerar PDF para Impressão"):
                try:
                    pdf_bytes = make_declaracao_pdf(
                        pivot_table, 
                        nao_vac, 
                        len(df_ativos_pdf), 
                        data_referencia
                    )
                    st.download_button(
                        label="⬇️ Baixar PDF (Declaração)",
                        data=pdf_bytes,
                        file_name=f"gedave_rebanho_{date.today()}.pdf",
                        mime="application/pdf"
                    )
                except Exception as e:
                    st.error(f"Erro ao gerar PDF: {e}")
                    logging.exception("PDF Error")
        
        with col_csv:
            csv_data = df_to_csv_bytes(df_view)
            st.download_button(
                label="⬇️ Baixar CSV (Dados Atuais)",
                data=csv_data,
                file_name="animais_export.csv",
                mime="text/csv"
            )

# =========================
# TAB 3: HISTÓRICO
# =========================
with tab3:
    st.header("Histórico Individual")
    
    # Selectbox para escolher animal
    df_animais = load_animais(st.session_state["cache_buster"])
    if df_animais.empty:
        st.info("Sem dados.")
    else:
        lista_animais = df_animais["brinco"].unique().tolist()
        lista_animais.sort()
        
        escolha = st.selectbox("Selecione o Brinco:", lista_animais)
        
        if escolha:
            # Pega ID
            animal_row = df_animais[df_animais["brinco"] == escolha].iloc[0]
            animal_id = int(animal_row["id"])
            
            # Mostra detalhes
            st.write(f"**Detalhes de {escolha}**")
            st.json({
                "Nascimento": str(animal_row["data_nascimento"]),
                "Sexo": animal_row["sexo"],
                "Status Atual": animal_row["status"],
                "Brucelose": "Sim" if animal_row["vacinada_brucelose"] else "Não"
            })
            
            st.subheader("Histórico de Movimentações")
            df_mov = load_movimentacoes(st.session_state["cache_buster"], animal_id)
            
            if not df_mov.empty:
                # Formata tabela
                df_show = df_mov[["data_evento", "tipo", "observacao"]].copy()
                df_show["data_evento"] = pd.to_datetime(df_show["data_evento"]).dt.strftime('%d/%m/%Y')
                st.dataframe(df_show, use_container_width=True, hide_index=True)
                
                # Botão de Excluir Animal (Perigoso)
                st.divider()
                with st.expander("Zona de Perigo"):
                    if st.button(f"Excluir Definitivamente o animal {escolha}"):
                        hard_delete_animal(escolha)
                        st.success("Animal excluído.")
                        st.rerun()
            else:
                st.info("Sem movimentações registradas.")
