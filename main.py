# ==============================================================================
# ||                   MEU GESTOR - BACKEND PRINCIPAL                     ||
# ==============================================================================
# Este arquivo contém toda a lógica para o assistente financeiro do WhatsApp,
# incluindo registro de despesas/créditos, resumos, edição e remoção.
# Testando o deploy da nova máquina

# --- Importações de Bibliotecas ---
import logging
import json
import os
import re
from datetime import datetime, date, timedelta
from typing import List, Tuple

# Terceiros
import requests
import openai
from dotenv import load_dotenv
from pydub import AudioSegment
from fastapi import FastAPI, Request, Depends
from sqlalchemy.orm import Session
from sqlalchemy import (create_engine, Column, Integer, String, Numeric,
                        DateTime, ForeignKey, func, and_)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.exc import SQLAlchemyError


# ==============================================================================
# ||                      CONFIGURAÇÃO E INICIALIZAÇÃO                        ||
# ==============================================================================

# Carrega variáveis de ambiente do arquivo .env
load_dotenv()

# Configuração do logging para observar o comportamento da aplicação
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Variáveis de Ambiente ---
DATABASE_URL = os.getenv("DATABASE_URL")
DIFY_API_URL = os.getenv("DIFY_API_URL")
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")

# --- Inicialização de APIs e Serviços ---
openai.api_key = OPENAI_API_KEY

if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
    AudioSegment.converter = FFMPEG_PATH
    logging.info(f"Pydub configurado para usar FFmpeg em: {FFMPEG_PATH}")
else:
    logging.warning("Caminho para FFMPEG_PATH não encontrado ou inválido. O processamento de áudio pode falhar.")

try:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()
    logging.info("Conexão com o banco de dados estabelecida com sucesso.")
except Exception as e:
    logging.error(f"Erro fatal ao conectar ao banco de dados: {e}")
    exit()


# ==============================================================================
# ||                      MODELOS DO BANCO DE DADOS (SQLALCHEMY)              ||
# ==============================================================================

class User(Base):
    """Modelo da tabela de usuários."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expenses = relationship("Expense", back_populates="user")
    incomes = relationship("Income", back_populates="user")
    reminders = relationship("Reminder", back_populates="user")

class Expense(Base):
    """Modelo da tabela de despesas."""
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)
    value = Column(Numeric(10, 2), nullable=False)
    category = Column(String)
    transaction_date = Column(DateTime, default=datetime.utcnow)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="expenses")

class Income(Base):
    """Modelo da tabela de rendas/créditos."""
    __tablename__ = "incomes"
    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)
    value = Column(Numeric(10, 2), nullable=False)
    transaction_date = Column(DateTime, default=datetime.utcnow)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="incomes")

class Reminder(Base):
    """Modelo da tabela de lembretes."""
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)
    due_date = Column(DateTime, nullable=False)
    is_sent = Column(String, default='false')
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="reminders")

# Cria as tabelas no banco de dados, se não existirem
Base.metadata.create_all(bind=engine)

def get_db():
    """Função de dependência do FastAPI para obter uma sessão de DB."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==============================================================================
# ||                   FUNÇÕES DE LÓGICA DE BANCO DE DADOS                    ||
# ==============================================================================

def get_or_create_user(db: Session, phone_number: str) -> User:
    """Busca um usuário pelo número de telefone ou cria um novo se não existir."""
    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user:
        logging.info(f"Criando novo usuário para o número: {phone_number}")
        user = User(phone_number=phone_number)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

def add_expense(db: Session, user: User, expense_data: dict):
    """Adiciona uma nova despesa para um usuário no banco de dados."""
    logging.info(f"Adicionando despesa para o usuário {user.id}...")
    new_expense = Expense(
        description=expense_data.get("description"),
        value=expense_data.get("value"),
        category=expense_data.get("category"),
        user_id=user.id
    )
    db.add(new_expense)
    db.commit()

def add_income(db: Session, user: User, income_data: dict):
    """Adiciona uma nova renda para um usuário no banco de dados."""
    logging.info(f"Adicionando renda para o usuário {user.id}...")
    new_income = Income(
        description=income_data.get("description"),
        value=income_data.get("value"),
        user_id=user.id
    )
    db.add(new_income)
    db.commit()

def add_reminder(db: Session, user: User, reminder_data: dict):
    """Adiciona um novo lembrete para um usuário no banco de dados."""
    logging.info(f"Adicionando lembrete para o usuário {user.id}...")
    new_reminder = Reminder(
        description=reminder_data.get("description"),
        due_date=reminder_data.get("due_date"),
        user_id=user.id
    )
    db.add(new_reminder)
    db.commit()

def get_expenses_summary(db: Session, user: User, period: str, category: str = None) -> Tuple[List[Expense], float] | None:
    """Busca a lista de despesas e o valor total para um período e categoria opcionais."""
    logging.info(f"Buscando resumo de despesas para o usuário {user.id}, período '{period}', categoria '{category}'")
    today = date.today()
    start_date = None
    period_lower = period.lower()

    if "mês" in period_lower: start_date = today.replace(day=1)
    elif "hoje" in period_lower: start_date = today
    elif "ontem" in period_lower:
        start_date = today - timedelta(days=1)
        end_date = today
        query = db.query(Expense).filter(Expense.user_id == user.id, Expense.transaction_date >= start_date, Expense.transaction_date < end_date)
        if category: query = query.filter(Expense.category == category)
        expenses = query.order_by(Expense.transaction_date.asc()).all()
        total_value = sum(expense.value for expense in expenses)
        return expenses, total_value
    elif "7 dias" in period_lower: start_date = today - timedelta(days=7)
    elif "30 dias" in period_lower: start_date = today - timedelta(days=30)
    
    if start_date:
        query = db.query(Expense).filter(Expense.user_id == user.id, Expense.transaction_date >= start_date)
        if category: query = query.filter(Expense.category == category)
        expenses = query.order_by(Expense.transaction_date.asc()).all()
        total_value = sum(expense.value for expense in expenses)
        return expenses, total_value
    
    return None

def get_incomes_summary(db: Session, user: User, period: str) -> Tuple[List[Income], float] | None:
    """Busca a lista de rendas e o valor total para um determinado período."""
    logging.info(f"Buscando resumo de créditos para o usuário {user.id} no período '{period}'")
    today = date.today()
    start_date = None
    period_lower = period.lower()

    if "mês" in period_lower: start_date = today.replace(day=1)
    elif "hoje" in period_lower: start_date = today
    elif "ontem" in period_lower:
        start_date = today - timedelta(days=1)
        end_date = today
        incomes = db.query(Income).filter(Income.user_id == user.id, Income.transaction_date >= start_date, Income.transaction_date < end_date).order_by(Income.transaction_date.asc()).all()
        total_value = sum(income.value for income in incomes)
        return incomes, total_value
    elif "7 dias" in period_lower: start_date = today - timedelta(days=7)
    elif "30 dias" in period_lower: start_date = today - timedelta(days=30)
    
    if start_date:
        incomes = db.query(Income).filter(Income.user_id == user.id, Income.transaction_date >= start_date).order_by(Income.transaction_date.asc()).all()
        total_value = sum(income.value for income in incomes)
        return incomes, total_value
    
    return None

def delete_last_expense(db: Session, user: User) -> dict | None:
    """Encontra e apaga a última despesa registrada por um usuário."""
    logging.info(f"Tentando apagar a última despesa do usuário {user.id}")
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    if last_expense:
        deleted_details = {"description": last_expense.description, "value": float(last_expense.value)}
        db.delete(last_expense)
        db.commit()
        return deleted_details
    return None

def edit_last_expense_value(db: Session, user: User, new_value: float) -> Expense | None:
    """Encontra e edita o valor da última despesa registrada por um usuário."""
    logging.info(f"Tentando editar o valor da última despesa do usuário {user.id} para {new_value}")
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    if last_expense:
        last_expense.value = new_value
        db.commit()
        db.refresh(last_expense)
        return last_expense
    return None


# ==============================================================================
# ||                   FUNÇÕES DE COMUNICAÇÃO COM APIS EXTERNAS               ||
# ==============================================================================

def transcribe_audio(file_path: str) -> str | None:
    """Transcreve um arquivo de áudio usando a API da OpenAI (Whisper)."""
    logging.info(f"Enviando áudio '{file_path}' para transcrição...")
    try:
        with open(file_path, "rb") as audio_file:
            transcription = openai.Audio.transcribe("whisper-1", audio_file)
        text = transcription["text"]
        logging.info(f"Transcrição bem-sucedida: '{text}'")
        return text
    except Exception as e:
        logging.error(f"Erro na transcrição com Whisper: {e}")
        return None

def call_dify_api(user_id: str, text_query: str) -> dict | None:
    """Envia uma consulta para o agente Dify e lida com respostas que não são JSON."""
    headers = {"Authorization": DIFY_API_KEY, "Content-Type": "application/json"}
    payload = {
        "inputs": {"query": text_query},
        "query": text_query,
        "user": user_id,
        "response_mode": "blocking"
    }
    try:
        logging.info(f"Payload enviado ao Dify:\n{json.dumps(payload, indent=2)}")
        response = requests.post(f"{DIFY_API_URL}/chat-messages", headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        answer_str = response.json().get("answer", "")
        try:
            return json.loads(answer_str)
        except json.JSONDecodeError:
            logging.warning(f"Dify retornou texto puro em vez de JSON: '{answer_str}'. Tratando como 'not_understood'.")
            return {"action": "not_understood"}
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro na chamada à API do Dify: {e.response.text if e.response else e}")
        return None

def send_whatsapp_message(phone_number: str, message: str):
    """Envia uma mensagem de texto via Evolution API."""
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    clean_number = phone_number.split('@')[0]
    payload = {"number": clean_number, "options": {"delay": 1200}, "text": message}
    try:
        logging.info(f"Enviando mensagem para {clean_number}: '{message}'")
        requests.post(url, headers=headers, json=payload, timeout=30).raise_for_status()
    except Exception as e:
        logging.error(f"Erro ao enviar mensagem via WhatsApp: {e}")


# ==============================================================================
# ||                         LÓGICA DE PROCESSAMENTO                          ||
# ==============================================================================

def process_text_message(message_text: str, sender_number: str) -> dict | None:
    """Processa uma mensagem de texto chamando a API do Dify."""
    logging.info(f">>> PROCESSANDO TEXTO: [{sender_number}]")
    dify_user_id = re.sub(r'\D', '', sender_number)
    return call_dify_api(user_id=dify_user_id, text_query=message_text)

def process_audio_message(message: dict, sender_number: str) -> dict | None:
    """Processa uma mensagem de áudio: baixa, converte, transcreve e envia para o Dify."""
    logging.info(f">>> PROCESSANDO ÁUDIO de [{sender_number}]")
    media_url = message.get("url") or message.get("mediaUrl")
    if not media_url:
        logging.warning("Mensagem de áudio sem URL.")
        return None

    mp3_file_path = f"temp_audio_{sender_number}.mp3"
    ogg_path = f"temp_audio_{sender_number}.ogg"
    
    try:
        response = requests.get(media_url, timeout=30)
        response.raise_for_status()
        with open(ogg_path, "wb") as f:
            f.write(response.content)
        AudioSegment.from_ogg(ogg_path).export(mp3_path, format="mp3")
        
        transcribed_text = transcribe_audio(mp3_file_path)
        if not transcribed_text:
            return None
        
        dify_user_id = re.sub(r'\D', '', sender_number)
        return call_dify_api(user_id=dify_user_id, text_query=transcribed_text)
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)
        if os.path.exists(mp3_file_path): os.remove(mp3_file_path)

def handle_dify_action(dify_result: dict, user: User, db: Session):
    """Executa a lógica apropriada baseada na ação retornada pelo Dify."""
    action = dify_result.get("action")
    sender_number = user.phone_number
    
    try:
        if action == "register_expense":
            add_expense(db, user=user, expense_data=dify_result)
            valor = float(dify_result.get('value', 0))
            descricao = dify_result.get('description', 'N/A')
            confirmation = f"✅ Despesa de R$ {valor:.2f} ({descricao}) registrada com sucesso!"
            send_whatsapp_message(sender_number, confirmation)

        elif action == "register_income":
            add_income(db, user=user, income_data=dify_result)
            valor = float(dify_result.get('value', 0))
            descricao = dify_result.get('description', 'N/A')
            confirmation = f"💰 Crédito de R$ {valor:.2f} ({descricao}) registrado com sucesso!"
            send_whatsapp_message(sender_number, confirmation)

        elif action == "create_reminder":
            add_reminder(db, user=user, reminder_data=dify_result)
            descricao = dify_result.get('description', 'N/A')
            due_date_str = dify_result.get('due_date')
            try:
                due_date_obj = datetime.fromisoformat(due_date_str)
                data_formatada = due_date_obj.strftime('%d/%m/%Y às %H:%M')
                confirmation = f"🗓️ Lembrete agendado: '{descricao}' para {data_formatada}."
            except (ValueError, TypeError):
                confirmation = f"🗓️ Lembrete '{descricao}' agendado com sucesso!"
            send_whatsapp_message(sender_number, confirmation)

        elif action == "get_summary":
            period = dify_result.get("period", "período não identificado")
            category = dify_result.get("category") # Pode ser None
            
            expense_data = get_expenses_summary(db, user=user, period=period, category=category)
            
            # O resumo de créditos não filtra por categoria de despesa
            income_data = get_incomes_summary(db, user=user, period=period)

            if expense_data is None or income_data is None:
                send_whatsapp_message(sender_number, f"Não consegui entender o período '{period}'. Tente 'hoje', 'ontem', ou 'este mês'.")
                return

            expenses, total_expenses = expense_data
            incomes, total_incomes = income_data
            balance = total_incomes - total_expenses

            # Formata os totais para o padrão brasileiro
            f_total_incomes = f"{total_incomes:.2f}".replace('.', ',')
            f_total_expenses = f"{total_expenses:.2f}".replace('.', ',')
            f_balance = f"{balance:.2f}".replace('.', ',')

            # Constrói a mensagem
            category_filter_text = f" de '{category}'" if category else ""
            summary_message = f"📊 *Balanço para '{period}'*:\n\n"
            
            if not category: # Só mostra o balanço completo se não houver filtro de categoria
                summary_message += f"💰 *Total de Créditos: R$ {f_total_incomes}*\n"
                if incomes:
                    for income in incomes[:3]:
                        summary_message += f"  - {income.description}\n"
                summary_message += "\n"

            summary_message += f"💸 *Total de Despesas{category_filter_text}: R$ {f_total_expenses}*\n"
            if expenses:
                for expense in expenses[:5]:
                    summary_message += f"  - {expense.description} (R$ {expense.value:.2f})\n"
            
            if not category: # Só mostra o balanço final se não houver filtro
                summary_message += f"\n--------------------\n"
                balance_emoji = "📈" if balance >= 0 else "📉"
                summary_message += f"{balance_emoji} *Balanço Final: R$ {f_balance}*"
            
            send_whatsapp_message(sender_number, summary_message)
        
        elif action == "delete_last_expense":
            deleted_expense = delete_last_expense(db, user=user)
            if deleted_expense:
                valor_f = deleted_expense.get('value', 0)
                descricao = deleted_expense.get('description', 'N/A')
                confirmation = f"🗑️ Despesa anterior ('{descricao}' de R$ {valor_f:.2f}) foi removida."
                send_whatsapp_message(sender_number, confirmation)
            else:
                send_whatsapp_message(sender_number, "🤔 Não encontrei nenhuma despesa para apagar.")
        
        elif action == "edit_last_expense_value":
            new_value = float(dify_result.get("new_value", 0))
            updated_expense = edit_last_expense_value(db, user=user, new_value=new_value)
            if updated_expense:
                descricao = updated_expense.description
                confirmation = f"✏️ Valor da despesa '{descricao}' corrigido para *R$ {updated_expense.value:.2f}*."
                send_whatsapp_message(sender_number, confirmation)
            else:
                send_whatsapp_message(sender_number, "🤔 Não encontrei nenhuma despesa para editar.")

        else: # "not_understood" ou qualquer outra ação
            fallback = "Não entendi. Tente de novo. Ex: 'gastei 50 no mercado', 'recebi 1000 de salário', 'resumo do mês'."
            send_whatsapp_message(sender_number, fallback)

    except Exception as e:
        logging.error(f"Erro ao manusear a ação '{action}': {e}")
        send_whatsapp_message(sender_number, "❌ Ocorreu um erro interno ao processar seu pedido.")


# ==============================================================================
# ||                          APLICAÇÃO FASTAPI (ROTAS)                         ||
# ==============================================================================

app = FastAPI()

@app.get("/")
def read_root():
    """Rota principal para verificar se o servidor está online."""
    return {"Status": "Meu Gestor Backend está online!"}

@app.post("/webhook/evolution")
async def evolution_webhook(request: Request, db: Session = Depends(get_db)):
    """Rota principal que recebe os webhooks da Evolution API."""
    data = await request.json()
    logging.info(f"DADOS RECEBIDOS: {json.dumps(data, indent=2)}")

    if data.get("event") != "messages.upsert":
        return {"status": "evento_ignorado"}
    message_data = data.get("data", {})
    if message_data.get("key", {}).get("fromMe"):
        return {"status": "mensagem_propria_ignorada"}
    
    sender_number = message_data.get("key", {}).get("remoteJid")
    message = message_data.get("message", {})
    if not sender_number or not message:
        return {"status": "dados_insuficientes"}

    dify_result = None
    if "conversation" in message and message["conversation"]:
        dify_result = process_text_message(message["conversation"], sender_number)
    elif "audioMessage" in message:
        dify_result = process_audio_message(message, sender_number)
    else:
        logging.info(f"Tipo de mensagem não suportado: {list(message.keys())}")
        return {"status": "tipo_nao_suportado"}

    if not dify_result:
        logging.warning("Sem resultado do Dify. Abortando.")
        return {"status": "falha_dify"}

    user = get_or_create_user(db, phone_number=sender_number)
    handle_dify_action(dify_result, user, db)

    return {"status": "processado"}


# Permite rodar o servidor com `python main.py` para desenvolvimento local
if __name__ == "__main__":