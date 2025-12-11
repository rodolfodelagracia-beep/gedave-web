import streamlit as st
import pandas as pd
import sqlite3
from datetime import date, datetime, timedelta
import random
import os

# --- CONFIGURAÇÃO DA PÁGINA ---
st.set_page_config(page_title="Gestão de Rebanho GEDAVE", layout="wide", page_icon="🐄")

# --- CONEXÃO COM BANCO DE DADOS ---
DB_FILE = "fazenda.db"

def get_connection():
    return sqlite3.connect(DB_FILE, check_same_thread=False)

def init_db():
    """Inicializa as tabelas se não existirem."""
    conn = get_connection()
    c = conn.cursor()
    # Tabela de Animais
    c.execute('''
        CREATE TABLE IF NOT EXISTS animais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brinco TEXT UNIQUE NOT NULL,
            data_nascimento DATE NOT NULL,
            sexo TEXT NOT NULL, -- 'M' ou 'F'
            data_entrada DATE NOT NULL,
            status TEXT DEFAULT 'Ativo', -- 'Ativo', 'Vendido', 'Morto'
            vacinada_brucelose BOOLEAN DEFAULT 0,
            data_referencia TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def seed_db():
    """Popula o banco com dados fictícios para teste se estiver vazio."""
    if os.path.exists(DB_FILE):
        conn = get_connection()
        c = conn.cursor()
        try:
            c.execute("SELECT COUNT(*) FROM animais")
            count = c.fetchone()[0]
            if count > 0:
                conn.close()
                return  # Já tem dados
        except:
            pass # Tabela pode não existir ainda
        finally:
             if conn: conn.close()

    # Gerar dados
    conn = get_connection()
    c = conn.cursor()
    
    sexos = ['M', 'F']
    hoje = date.today()
    
    # Estratégia de Seed para cobrir todas as faixas do GEDAVE
    offsets = [
        30,      # ~1 mês (0-2)
        120,     # ~4 meses (3-8) -> Fêmea aqui é CRÍTICA para Brucelose
        300,     # ~10 meses (9-12)
        500,     # ~1.5 anos (13-24)
        900,     # ~2.5 anos (25-36)
        1200     # ~3+ anos (>36)
    ]
    
    animais_fake = []
    
    for i, dias in enumerate(offsets):
        nasc = hoje - timedelta(days=dias)
        # 3 animais por faixa/tipo base
        for k in range(3):
            sexo = random.choice(['M', 'F'])
            
            # Forçar pelo menos uma Fêmea de 4 meses NÃO VACINADA para teste de alerta
            if dias == 120 and k == 0: 
                sexo = 'F'
                vacinada = False
            else:
                vacinada = random.choice([True, False]) if sexo == 'F' else False
                
            brinco = f"TESTE-{i}-{k}-{random.randint(100,999)}"
            animais_fake.append((brinco, nasc, sexo, hoje, 'Ativo', vacinada))

    try:
        c.executemany("INSERT INTO animais (brinco, data_nascimento, sexo, data_entrada, status, vacinada_brucelose) VALUES (?, ?, ?, ?, ?, ?)", animais_fake)
        conn.commit()
        st.toast("Banco de dados populado com dados de teste!", icon="✅")
    except Exception as e:
        pass # Ignora erro se já existir (duplicação)
    finally:
        conn.close()

# --- INICIALIZAÇÃO ---
init_db()
seed_db()

# --- FUNÇÕES DE NEGÓCIO ---

def calcular_idade_meses(data_nasc, data_ref):
    """Calcula idade em meses completos."""
    if isinstance(data_nasc, str):
        data_nasc = datetime.strptime(data_nasc, "%Y-%m-%d").date()
    
    # Diferença em meses (aproximação precisa o suficiente para GEDAVE)
    delta = data_ref.year * 12 + data_ref.month - (data_nasc.year * 12 + data_nasc.month)
    
    # Ajuste fino: se o dia de referência for menor que o dia de nascimento, ainda não completou o mês
    if data_ref.day < data_nasc.day:
        delta -= 1
        
    return max(0, delta)

def classificar_faixa(meses):
    if 0 <= meses < 3: return "00 a 02 meses"
    if 3 <= meses < 9: return "03 a 08 meses"
    if 9 <= meses < 13: return "09 a 12 meses"
    if 13 <= meses < 25: return "13 a 24 meses"
    if 25 <= meses < 37: return "25 a 36 meses"
    if meses >= 37: return "Acima de 36 meses"
    return "Erro"

SEXO_MAP = {'M': 'Macho', 'F': 'Fêmea'}

# --- UI PRINCIPAL ---

st.title("🚜 Gestão de Rebanho - GEDAVE SP")

# Sidebar
with st.sidebar:
    st.header("Configurações")
    data_referencia = st.date_input("Data de Referência (Cálculo Idade)", value=date.today())
    st.info(f"Idades calculadas baseadas em: **{data_referencia.strftime('%d/%m/%Y')}**")
    
    st.markdown("---")
    if st.button("Resetar Banco de Dados (Apagar Tudo)"):
        if os.path.exists(DB_FILE):
            try:
                os.remove(DB_FILE)
                st.rerun()
            except:
                st.error("Erro ao apagar arquivo. Tente reiniciar a aplicação.")

# Abas
tab1, tab2 = st.tabs(["📝 Cadastro e Movimentação", "📊 Painel GEDAVE"])

# --- ABA 1: CADASTRO ---
with tab1:
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.subheader("Novo Animal")
        # --- FORMULÁRIO (INDENTADO) ---
        with st.form("form_animal"):
            brinco_input = st.text_input("Brinco (ID)")
            nasc_input = st.date_input("Data Nascimento")
            sexo_input = st.radio("Sexo", ["M", "F"], horizontal=True)
            entrada_input = st.date_input("Data Entrada", value=date.today())
            vacinada_input = st.checkbox("Vacinada Brucelose?", value=False)
            
            submit = st.form_submit_button("Salvar Animal")
            
            if submit and brinco_input:
                conn = get_connection()
                try:
                    conn.execute(
                        "INSERT INTO animais (brinco, data_nascimento, sexo, data_entrada, vacinada_brucelose) VALUES (?, ?, ?, ?, ?)",
                        (brinco_input, nasc_input, sexo_input, entrada_input, vacinada_input)
                    )
                    conn.commit()
                    st.success(f"Animal {brinco_input} salvo!")
                except sqlite3.IntegrityError:
                    st.error("Erro: Brinco já existe!")
                finally:
                    conn.close()
        # --- FIM DO FORMULÁRIO ---
        
        st.divider()
        
        # --- ZONA DE PERIGO (FORA DO FORMULÁRIO - CORRIGIDO) ---
        with st.expander("🗑️ Zona de Perigo (Excluir Animal)"):
            brinco_del = st.text_input("Digite o Brinco para excluir")
            if st.button("Excluir Definitivamente"):
                if brinco_del:
                    conn = get_connection()
                    try:
                        c = conn.cursor()
                        c.execute("DELETE FROM animais WHERE brinco = ?", (brinco_del,))
                        if c.rowcount > 0:
                            conn.commit()
                            st.warning(f"Animal {brinco_del} foi apagado!")
                            # Pequeno delay ou rerun direto
                            st.rerun()
                        else:
                            st.error("Brinco não encontrado.")
                    except Exception as e:
                        st.error(f"Erro: {e}")
                    finally:
                        conn.close()

    with col2:
        st.subheader("Animais Ativos")
        conn = get_connection()
        df_animais = pd.read_sql("SELECT * FROM animais WHERE status='Ativo'", conn)
        conn.close()
        
        if not df_animais.empty:
            # Edição Rápida
            edited_df = st.data_editor(
                df_animais,
                column_config={
                    "vacinada_brucelose": st.column_config.CheckboxColumn("Vacina Brucelose", help="Apenas Fêmeas"),
                    "status": st.column_config.SelectboxColumn("Status", options=['Ativo', 'Vendido', 'Morto', 'Roubado']),
                },
                disabled=["id", "data_referencia"],
                hide_index=True,
                key="editor_animais"
            )
            
            if st.button("💾 Salvar Alterações na Tabela"):
                conn = get_connection()
                for index, row in edited_df.iterrows():
                    conn.execute("""
                        UPDATE animais 
                        SET status = ?, vacinada_brucelose = ? 
                        WHERE id = ?
                    """, (row['status'], row['vacinada_brucelose'], row['id']))
                conn.commit()
                conn.close()
                st.success("Tabela atualizada com sucesso!")
                st.rerun()

# --- ABA 2: GEDAVE ---
with tab2:
    st.header("Declaração de Rebanho")
    
    conn = get_connection()
    df = pd.read_sql("SELECT * FROM animais WHERE status='Ativo'", conn)
    conn.close()
    
    if df.empty:
        st.warning("Nenhum animal ativo encontrado.")
    else:
        # 1. Calcular Idade Real na Data de Referência
        df['dt_nasc'] = pd.to_datetime(df['data_nascimento']).dt.date
        df['meses_idade'] = df['dt_nasc'].apply(lambda x: calcular_idade_meses(x, data_referencia))
        
        # 2. Classificação
        df['Faixa Etária'] = df['meses_idade'].apply(classificar_faixa)
        df['Sexo_Label'] = df['sexo'].map(SEXO_MAP)
        
        # 3. Pivot Table (Tabela Cruzada)
        faixas_ordem = [
            "00 a 02 meses", "03 a 08 meses", "09 a 12 meses", 
            "13 a 24 meses", "25 a 36 meses", "Acima de 36 meses"
        ]
        
        pivot = pd.crosstab(
            index=df['Sexo_Label'], 
            columns=df['Faixa Etária']
        )
        
        pivot = pivot.reindex(columns=faixas_ordem, fill_value=0)
        
        st.subheader("1. Saldo de Rebanho (Por Faixa Etária e Sexo)")
        st.dataframe(pivot, use_container_width=True)
        
        total_animais = len(df)
        st.metric("Total de Cabeças", total_animais)

        st.divider()

        st.subheader("2. Controle de Brucelose (Fêmeas 03 a 08 meses)")
        
        femeas_brucelose = df[
            (df['sexo'] == 'F') & 
            (df['meses_idade'] >= 3) & 
            (df['meses_idade'] < 9)
        ].copy()
        
        if femeas_brucelose.empty:
            st.info("Nenhuma fêmea na idade de vacinação (3 a 8 meses).")
        else:
            col_b1, col_b2 = st.columns(2)
            
            with col_b1:
                st.markdown("#### Status Vacinal")
                stats_brucelose = femeas_brucelose['vacinada_brucelose'].value_counts().rename(index={0: 'Não Vacinada', 1: 'Vacinada'})
                st.dataframe(stats_brucelose)
            
            with col_b2:
                nao_vacinadas = femeas_brucelose[femeas_brucelose['vacinada_brucelose'] == 0]
                count_nao = len(nao_vacinadas)
                
                if count_nao > 0:
                    st.error(f"⚠️ ATENÇÃO: {count_nao} fêmea(s) não vacinada(s) nesta faixa!")
                    st.dataframe(nao_vacinadas[['brinco', 'meses_idade', 'vacinada_brucelose']], hide_index=True)
                else:
                    st.success("✅ Todas as fêmeas desta faixa estão vacinadas!")
