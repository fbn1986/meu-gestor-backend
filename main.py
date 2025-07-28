# ==============================================================================
# ||                      MEU GESTOR - BACKEND PRINCIPAL (com API)                      ||
# ==============================================================================
# VERSÃO 12.2: Corrige erro de CORS e 500 Internal Server Error na rota de dados.

# --- Importações de Bibliotecas ---
import logging
import json
import os
import re
import secrets
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional

# Terceiros
import requests
import openai
from dotenv import load_dotenv
from pydub import AudioSegment
from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import (create_engine, Column, Integer, String, Numeric,
                        DateTime, ForeignKey, func, and_, Boolean)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.exc import SQLAlchemyError


# ==============================================================================
# ||                      CONFIGURAÇÃO E INICIALIZAÇÃO                      ||
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
DASHBOARD_URL = os.getenv("DASHBOARD_URL")
CRON_SECRET_KEY = os.getenv("CRON_SECRET_KEY")

# --- Constantes de Fuso Horário ---
TZ_UTC = ZoneInfo("UTC")
TZ_SAO_PAULO = ZoneInfo("America/Sao_Paulo")


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
# ||                   MODELOS DO BANCO DE DADOS (SQLALCHEMY)                   ||
# ==============================================================================
class User(Base):
    """Modelo da tabela de usuários."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(TZ_UTC))
    expenses = relationship("Expense", back_populates="user")
    incomes = relationship("Income", back_populates="user")
    reminders = relationship("Reminder", back_populates="user", cascade="all, delete-orphan")
    auth_tokens = relationship("AuthToken", back_populates="user")
    categories = relationship("Category", back_populates="user", cascade="all, delete-orphan")

class Expense(Base):
    """Modelo da tabela de despesas."""
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)
    value = Column(Numeric(10, 2), nullable=False)
    category = Column(String)
    transaction_date = Column(DateTime(timezone=True), default=lambda: datetime.now(TZ_UTC))
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="expenses")

class Income(Base):
    """Modelo da tabela de rendas/créditos."""
    __tablename__ = "incomes"
    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)
    value = Column(Numeric(10, 2), nullable=False)
    transaction_date = Column(DateTime(timezone=True), default=lambda: datetime.now(TZ_UTC))
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="incomes")

class Reminder(Base):
    """Modelo da tabela de lembretes, agora com suporte a recorrência."""
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    
    # --- Campos para lembretes PONTUAIS ---
    due_date = Column(DateTime(timezone=True), nullable=True) # Data e hora exata
    is_sent = Column(Boolean, default=False) # Se a notificação pontual foi enviada
    
    # --- Campos para lembretes RECORRENTES ---
    is_recurring = Column(Boolean, default=False)
    day_of_month = Column(Integer, nullable=True) # Dia do vencimento (ex: 10)
    notification_day_offset = Column(Integer, default=5) # Dias de antecedência para notificar
    last_triggered_month = Column(Integer, nullable=True) # Mês do último disparo para evitar duplicatas
    last_triggered_year = Column(Integer, nullable=True) # Ano do último disparo
    
    user = relationship("User", back_populates="reminders")

class AuthToken(Base):
    """Modelo para tokens de autenticação temporários."""
    __tablename__ = "auth_tokens"
    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    expires_at = Column(DateTime(timezone=True), nullable=False)
    user = relationship("User", back_populates="auth_tokens")

class Category(Base):
    """Modelo para categorias personalizadas de usuários."""
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="categories")

# Cria as tabelas no banco de dados, se não existirem
Base.metadata.create_all(bind=engine)

# --- Modelos Pydantic para validação de dados da API ---
class ExpenseUpdate(BaseModel):
    description: str
    value: float
    category: Optional[str] = None

class IncomeUpdate(BaseModel):
    description: str
    value: float

class CategoryCreate(BaseModel):
    name: str

class CategoryUpdate(BaseModel):
    name: str

class ReminderUpdate(BaseModel):
    description: str
    due_date: str # Receber como string ISO e converter

class RecurringReminderCreate(BaseModel):
    description: str
    day_of_month: int
    notification_day_offset: Optional[int] = 5

class RecurringReminderUpdate(BaseModel):
    description: str
    day_of_month: int
    notification_day_offset: int

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

def create_auth_token(db: Session, user: User) -> str:
    """Cria e armazena um token de autenticação temporário para um usuário."""
    token_str = secrets.token_urlsafe(16)
    expires = datetime.now(TZ_UTC) + timedelta(minutes=5)
    token = AuthToken(token=token_str, user_id=user.id, expires_at=expires)
    db.add(token)
    db.commit()
    return token_str

def add_expense(db: Session, user: User, expense_data: dict):
    """Adiciona uma nova despesa para um usuário no banco de dados."""
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
    new_income = Income(
        description=income_data.get("description"),
        value=income_data.get("value"),
        user_id=user.id
    )
    db.add(new_income)
    db.commit()

def add_reminder(db: Session, user: User, reminder_data: dict):
    """Adiciona um novo lembrete PONTUAL para um usuário no banco de dados."""
    new_reminder = Reminder(
        description=reminder_data.get("description"),
        due_date=reminder_data.get("due_date"),
        user_id=user.id,
        is_recurring=False
    )
    db.add(new_reminder)
    db.commit()

def add_recurring_reminder(db: Session, user: User, reminder_data: dict):
    """Adiciona um novo lembrete RECORRENTE para um usuário no banco de dados."""
    new_reminder = Reminder(
        description=reminder_data.get("description"),
        day_of_month=reminder_data.get("day_of_month"),
        notification_day_offset=reminder_data.get("notification_day_offset", 5),
        user_id=user.id,
        is_recurring=True
    )
    db.add(new_reminder)
    db.commit()

def get_user_categories(db: Session, user: User) -> List[dict]:
    """Busca todas as categorias de um usuário (padrão e personalizadas)."""
    default_categories = [
        {"id": f"default_{i}", "name": name, "is_default": True}
        for i, name in enumerate(["Alimentação", "Transporte", "Moradia", "Lazer", "Saúde", "Educação", "Outros"])
    ]
    custom_categories = [
        {"id": c.id, "name": c.name, "is_default": False}
        for c in db.query(Category).filter(Category.user_id == user.id).order_by(Category.name).all()
    ]
    return custom_categories + default_categories

def create_user_category(db: Session, user: User, category_name: str) -> Category:
    """Cria uma nova categoria para um usuário."""
    new_category = Category(name=category_name, user_id=user.id)
    db.add(new_category)
    db.commit()
    db.refresh(new_category)
    return new_category

def delete_user_category(db: Session, user: User, category_name: str) -> bool:
    """Apaga uma categoria personalizada de um usuário."""
    category_to_delete = db.query(Category).filter(
        func.lower(Category.name) == func.lower(category_name),
        Category.user_id == user.id
    ).first()
    if category_to_delete:
        db.delete(category_to_delete)
        db.commit()
        return True
    return False

def get_expenses_summary(db: Session, user: User, period: str, category: str = None) -> Tuple[List[Expense], float, datetime, datetime] | None:
    """Busca a lista de despesas, o valor total e o intervalo de datas para um período."""
    now_brt = datetime.now(TZ_SAO_PAULO)
    start_of_today_brt = now_brt.replace(hour=0, minute=0, second=0, microsecond=0)
    
    start_brt, end_brt = None, None
    period_lower = period.lower()
    dynamic_days_match = re.search(r'últimos (\d+) dias', period_lower)

    if "mês" in period_lower:
        start_brt = start_of_today_brt.replace(day=1)
        end_brt = start_of_today_brt + timedelta(days=1)
    elif "hoje" in period_lower:
        start_brt = start_of_today_brt
        end_brt = start_of_today_brt + timedelta(days=1)
    elif "ontem" in period_lower:
        start_brt = start_of_today_brt - timedelta(days=1)
        end_brt = start_of_today_brt
    elif "semana" in period_lower or "7 dias" in period_lower:
        start_brt = start_of_today_brt - timedelta(days=6)
        end_brt = start_of_today_brt + timedelta(days=1)
    elif dynamic_days_match:
        days = int(dynamic_days_match.group(1))
        start_brt = start_of_today_brt - timedelta(days=days - 1)
        end_brt = start_of_today_brt + timedelta(days=1)
    
    if start_brt and end_brt:
        start_utc = start_brt.astimezone(TZ_UTC)
        end_utc = end_brt.astimezone(TZ_UTC)
        query = db.query(Expense).filter(
            Expense.user_id == user.id,
            Expense.transaction_date >= start_utc,
            Expense.transaction_date < end_utc
        )
        if category:
            query = query.filter(func.lower(Expense.category) == func.lower(category))
            
        expenses = query.order_by(Expense.transaction_date.asc()).all()
        total_value = sum(expense.value for expense in expenses)
        return expenses, total_value, start_brt, end_brt
    
    return None, 0.0, None, None

def get_incomes_summary(db: Session, user: User, period: str) -> Tuple[List[Income], float] | None:
    """Busca a lista de rendas e o valor total para um determinado período."""
    now_brt = datetime.now(TZ_SAO_PAULO)
    start_of_today_brt = now_brt.replace(hour=0, minute=0, second=0, microsecond=0)

    start_brt, end_brt = None, None
    period_lower = period.lower()
    dynamic_days_match = re.search(r'últimos (\d+) dias', period_lower)

    if "mês" in period_lower:
        start_brt = start_of_today_brt.replace(day=1)
        end_brt = start_of_today_brt + timedelta(days=1)
    elif "hoje" in period_lower:
        start_brt = start_of_today_brt
        end_brt = start_of_today_brt + timedelta(days=1)
    elif "ontem" in period_lower:
        start_brt = start_of_today_brt - timedelta(days=1)
        end_brt = start_of_today_brt
    elif "semana" in period_lower or "7 dias" in period_lower:
        start_brt = start_of_today_brt - timedelta(days=6)
        end_brt = start_of_today_brt + timedelta(days=1)
    elif dynamic_days_match:
        days = int(dynamic_days_match.group(1))
        start_brt = start_of_today_brt - timedelta(days=days - 1)
        end_brt = start_of_today_brt + timedelta(days=1)

    if start_brt and end_brt:
        start_utc = start_brt.astimezone(TZ_UTC)
        end_utc = end_brt.astimezone(TZ_UTC)
        query = db.query(Income).filter(
            Income.user_id == user.id,
            Income.transaction_date >= start_utc,
            Income.transaction_date < end_utc
        )
            
        incomes = query.order_by(Income.transaction_date.asc()).all()
        total_value = sum(income.value for income in incomes)
        return incomes, total_value
    
    return None, 0.0

def get_reminders_for_period(db: Session, user: User, period: str) -> Tuple[List[Reminder], Optional[datetime], Optional[datetime]]:
    """Busca lembretes PONTUAIS para um determinado período."""
    now_brt = datetime.now(TZ_SAO_PAULO)
    start_of_today_brt = now_brt.replace(hour=0, minute=0, second=0, microsecond=0)
    
    start_brt, end_brt = None, None
    period_lower = period.lower()
    date_match = re.search(r'(\d{2}/\d{2}/\d{4})', period_lower)
    
    if "hoje" in period_lower:
        start_brt = start_of_today_brt
        end_brt = start_brt + timedelta(days=1)
    elif "amanhã" in period_lower:
        start_brt = start_of_today_brt + timedelta(days=1)
        end_brt = start_brt + timedelta(days=1)
    elif date_match:
        date_str = date_match.group(1)
        try:
            day_brt = datetime.strptime(date_str, '%d/%m/%Y')
            start_brt = day_brt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ_SAO_PAULO)
            end_brt = start_brt + timedelta(days=1)
        except ValueError:
            return [], None, None
    
    if start_brt and end_brt:
        start_utc = start_brt.astimezone(TZ_UTC)
        end_utc = end_brt.astimezone(TZ_UTC)
        reminders = db.query(Reminder).filter(
            Reminder.user_id == user.id,
            Reminder.is_recurring == False,
            Reminder.due_date >= start_utc,
            Reminder.due_date < end_utc
        ).order_by(Reminder.due_date.asc()).all()
        return reminders, start_brt, end_brt
    
    return [], None, None

def delete_last_expense(db: Session, user: User) -> dict | None:
    """Encontra e apaga a última despesa registrada por um usuário."""
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    if last_expense:
        deleted_details = {"description": last_expense.description, "value": float(last_expense.value)}
        db.delete(last_expense)
        db.commit()
        return deleted_details
    return None

def edit_last_expense_value(db: Session, user: User, new_value: float) -> Expense | None:
    """Encontra e edita o valor da última despesa registrada por um usuário."""
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    if last_expense:
        last_expense.value = new_value
        db.commit()
        db.refresh(last_expense)
        return last_expense
    return None


# ==============================================================================
# ||                   FUNÇÕES DE COMUNICAÇÃO COM APIS EXTERNAS                 ||
# ==============================================================================

def transcribe_audio(file_path: str) -> str | None:
    """Transcreve um arquivo de áudio usando a API da OpenAI (Whisper)."""
    try:
        with open(file_path, "rb") as audio_file:
            transcription = openai.Audio.transcribe("whisper-1", audio_file)
        text = transcription["text"]
        return text
    except Exception as e:
        logging.error(f"Erro na transcrição com Whisper: {e}")
        return None

def call_dify_api(user_id: str, text_query: str, file_id: Optional[str] = None) -> dict | None:
    """Envia uma consulta para o agente Dify, incluindo um file_id se fornecido."""
    headers = {"Authorization": DIFY_API_KEY, "Content-Type": "application/json"}
    payload = {
        "inputs": {},
        "query": text_query,
        "user": user_id,
        "response_mode": "blocking"
    }
    if file_id:
        payload["files"] = [{"type": "image", "transfer_method": "local_file", "upload_file_id": file_id}]

    try:
        logging.info(f"Payload enviado ao Dify: {json.dumps(payload, indent=2)}")
        response = requests.post(f"{DIFY_API_URL}/chat-messages", headers=headers, json=payload, timeout=180)
        response.raise_for_status()
        answer_str = response.json().get("answer", "")
        try:
            return json.loads(answer_str)
        except json.JSONDecodeError:
            logging.warning(f"Dify retornou texto puro em vez de JSON: '{answer_str}'.")
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
# ||                         LÓGICA DE PROCESSAMENTO                        ||
# ==============================================================================

def process_text_message(message_text: str, sender_number: str, db: Session) -> dict | None:
    """Processa uma mensagem de texto chamando a API do Dify."""
    logging.info(f">>> PROCESSANDO TEXTO: [{sender_number}]")
    dify_user_id = re.sub(r'\D', '', sender_number)
    user = get_or_create_user(db, sender_number)
    
    if any(keyword in message_text.lower() for keyword in ["gastei", "comprei", "paguei", "despesa"]):
        user_categories = [c['name'] for c in get_user_categories(db, user)]
        category_list_str = ", ".join(user_categories)
        enriched_query = f"{message_text}. Contexto Adicional: Para o campo 'category', use uma das seguintes opções: {category_list_str}."
        return call_dify_api(user_id=dify_user_id, text_query=enriched_query)
        
    return call_dify_api(user_id=dify_user_id, text_query=message_text)

def process_audio_message(message: dict, sender_number: str, db: Session) -> dict | None:
    """Processa uma mensagem de áudio: baixa, converte, transcreve e envia para o Dify."""
    logging.info(f">>> PROCESSANDO ÁUDIO de [{sender_number}]")
    media_url = message.get("url") or message.get("mediaUrl")
    if not media_url:
        return None

    mp3_file_path = f"temp_audio_{sender_number}.mp3"
    ogg_path = f"temp_audio_{sender_number}.ogg"
    
    try:
        response = requests.get(media_url, timeout=30)
        response.raise_for_status()
        with open(ogg_path, "wb") as f:
            f.write(response.content)
        AudioSegment.from_ogg(ogg_path).export(mp3_file_path, format="mp3")
        
        transcribed_text = transcribe_audio(mp3_file_path)
        if not transcribed_text:
            return None
        
        return process_text_message(transcribed_text, sender_number, db)
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)
        if os.path.exists(mp3_file_path): os.remove(mp3_file_path)

def process_image_message(message: dict, sender_number: str) -> dict | None:
    """Processa uma mensagem de imagem: baixa, envia para o Dify files e depois para o chat."""
    logging.info(f">>> PROCESSANDO IMAGEM de [{sender_number}]")
    media_url = message.get("mediaUrl") or message.get("url")
    if not media_url:
        return None

    try:
        response = requests.get(media_url, timeout=30)
        response.raise_for_status()
        image_content = response.content
        
        dify_user_id = re.sub(r'\D', '', sender_number)
        upload_url = f"{DIFY_API_URL}/files/upload"
        headers = {"Authorization": DIFY_API_KEY}
        files = {'file': ('image.jpeg', image_content, 'image/jpeg')}
        data = {'user': dify_user_id}
        
        upload_response = requests.post(upload_url, headers=headers, files=files, data=data, timeout=60)
        upload_response.raise_for_status()
        upload_result = upload_response.json()
        file_id = upload_result.get('id')

        if not file_id:
            return None

        prompt = "Analise este cupom fiscal e registre a despesa."
        return call_dify_api(user_id=dify_user_id, text_query=prompt, file_id=file_id)
        
    except Exception as e:
        logging.error(f"Erro ao processar imagem: {e}")
        return None

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
            descricao = dify_result.get('description', 'N/A')
            due_date_str = dify_result.get('due_date')
            try:
                # CORREÇÃO DE TIMEZONE: Assume que a string do Dify é a hora local e a torna "aware"
                naive_datetime = datetime.fromisoformat(due_date_str)
                aware_brt_datetime = naive_datetime.replace(tzinfo=TZ_SAO_PAULO)
                
                dify_result['due_date'] = aware_brt_datetime
                add_reminder(db, user=user, reminder_data=dify_result)
                
                data_formatada = aware_brt_datetime.strftime('%d/%m/%Y às %H:%M')
                confirmation = f"🗓️ Lembrete agendado: '{descricao}' para {data_formatada}."
            except (ValueError, TypeError):
                # Fallback caso a data venha em formato inesperado
                add_reminder(db, user=user, reminder_data=dify_result)
                confirmation = f"🗓️ Lembrete '{descricao}' agendado com sucesso!"
            send_whatsapp_message(sender_number, confirmation)

        elif action == "create_recurring_reminder":
            descricao = dify_result.get('description', 'N/A')
            day_of_month = dify_result.get('day_of_month')
            
            if not descricao or not day_of_month:
                send_whatsapp_message(sender_number, "🤔 Não consegui entender os detalhes do lembrete recorrente. Tente algo como 'lembrete recorrente de aluguel todo dia 5'.")
                return

            try:
                day_of_month = int(day_of_month)
                if not 1 <= day_of_month <= 31:
                    raise ValueError("Dia do mês inválido")

                add_recurring_reminder(db, user=user, reminder_data={"description": descricao, "day_of_month": day_of_month})
                confirmation = f"🔁 Lembrete recorrente criado: '{descricao}', todo dia {day_of_month}."
                send_whatsapp_message(sender_number, confirmation)
            except (ValueError, TypeError):
                send_whatsapp_message(sender_number, f"🤔 O dia '{day_of_month}' não parece ser um dia válido. Por favor, forneça um número de 1 a 31.")

        elif action == "get_dashboard_link":
            if not DASHBOARD_URL:
                send_whatsapp_message(sender_number, "Desculpe, a funcionalidade de link para o painel não está configurada.")
                return
            
            token = create_auth_token(db, user)
            login_url = f"{DASHBOARD_URL}?token={token}"
            message = f"Olá! Acesse seu painel de controle pessoal aqui: {login_url}"
            send_whatsapp_message(sender_number, message)

        elif action == "get_summary":
            period = dify_result.get("period", "período não identificado")
            category = dify_result.get("category")
            
            expense_data = get_expenses_summary(db, user=user, period=period, category=category)
            if expense_data is None or expense_data[2] is None:
                send_whatsapp_message(sender_number, f"Não consegui entender o período '{period}'. Tente 'hoje', 'ontem', 'este mês', ou 'últimos X dias'.")
                return
            expenses, total_expenses, start_date, end_date = expense_data

            income_data = get_incomes_summary(db, user=user, period=period)
            incomes, total_incomes = (income_data if income_data else ([], 0.0))
            
            balance = total_incomes - total_expenses

            start_date_str = start_date.strftime('%d/%m/%Y')
            end_date_str = (end_date - timedelta(days=1)).strftime('%d/%m/%Y')

            summary_message = f"Vamos lá! No período de {start_date_str} a {end_date_str}, este é o seu balanço:\n\n"

            f_total_incomes = f"{total_incomes:.2f}".replace('.', ',')
            summary_message += f"💰 *Créditos: R$ {f_total_incomes}*\n"
            if incomes:
                for income in incomes:
                    date_str = income.transaction_date.astimezone(TZ_SAO_PAULO).strftime('%d/%m/%Y')
                    f_income_value = f"{income.value:.2f}".replace('.', ',')
                    summary_message += f"- {date_str}: {income.description} - R$ {f_income_value}\n"
            else:
                summary_message += "- Nenhum crédito no período.\n"
            summary_message += "\n"

            summary_message += "💸 *Despesas*\n"
            if not expenses:
                summary_message += "- Nenhuma despesa no período. 🎉\n"
            else:
                expenses_by_category = {}
                category_emojis = { "Alimentação": "🍽️", "Transporte": "🚗", "Moradia": "🏠", "Lazer": "🎉", "Saúde": "❤️‍🩹", "Educação": "🎓", "Outros": "🛒" }

                for expense in expenses:
                    cat = expense.category if expense.category else "Outros"
                    if cat not in expenses_by_category:
                        expenses_by_category[cat] = {"items": [], "total": 0}
                    expenses_by_category[cat]["items"].append(expense)
                    expenses_by_category[cat]["total"] += expense.value

                sorted_categories = sorted(expenses_by_category.items(), key=lambda item: item[1]['total'], reverse=True)

                for cat, data in sorted_categories:
                    emoji = category_emojis.get(cat, "🛒")
                    summary_message += f"\n{emoji} *{cat}*\n"
                    for expense in data["items"]:
                        date_str = expense.transaction_date.astimezone(TZ_SAO_PAULO).strftime('%d/%m/%Y')
                        f_expense_value = f"{expense.value:.2f}".replace('.', ',')
                        summary_message += f"- {date_str}: {expense.description} - R$ {f_expense_value}\n"
                    
                    f_cat_total = f"{data['total']:.2f}".replace('.', ',')
                    summary_message += f"*Subtotal {cat}: R$ {f_cat_total}*\n"
            
            f_balance = f"{balance:.2f}".replace('.', ',')
            balance_emoji = "📈" if balance >= 0 else "📉"
            summary_message += f"\n--------------------\n"
            summary_message += f"{balance_emoji} *Balanço Final: R$ {f_balance}*\n\n"
            
            if DASHBOARD_URL:
                token = create_auth_token(db, user)
                login_url = f"{DASHBOARD_URL}?token={token}"
                summary_message += f"Para mais detalhes, acesse seu painel: {login_url} 😉"
            
            send_whatsapp_message(sender_number, summary_message)
        
        elif action == "get_reminders":
            period = dify_result.get("period", "hoje")
            reminders, start_date, _ = get_reminders_for_period(db, user, period)

            if not start_date:
                send_whatsapp_message(sender_number, f"Não consegui entender o período '{period}' para os lembretes.")
                return

            period_display_name = period
            if re.search(r'(\d{2}/\d{2}/\d{4})', period):
                period_display_name = f"o dia {period}"

            if not reminders:
                message = f"Você não tem nenhum compromisso agendado para {period_display_name}! 👍"
            else:
                message = f"🗓️ Você tem {len(reminders)} compromisso(s) para {period_display_name}!\n\n"
                for r in reminders:
                    due_time_brt = r.due_date.astimezone(TZ_SAO_PAULO).strftime('%H:%M')
                    message += f"• {r.description} às {due_time_brt} horas.\n"
                message += "\nNão se preocupe, estarei aqui para te lembrar se precisar! 😉"
            
            send_whatsapp_message(sender_number, message)

        elif action == "create_category":
            category_name = dify_result.get("category_name")
            if category_name:
                create_user_category(db, user, category_name)
                send_whatsapp_message(sender_number, f"✅ Categoria '{category_name}' criada com sucesso!")
            else:
                send_whatsapp_message(sender_number, "🤔 Não consegui identificar o nome da categoria.")

        elif action == "list_categories":
            categories = get_user_categories(db, user)
            message = "📋 *Suas Categorias:*\n\n"
            for cat in categories:
                message += f"• {cat['name']}\n"
            send_whatsapp_message(sender_number, message)

        elif action == "delete_category":
            category_name = dify_result.get("category_name")
            if category_name:
                if delete_user_category(db, user, category_name):
                    send_whatsapp_message(sender_number, f"🗑️ Categoria '{category_name}' apagada com sucesso.")
                else:
                    send_whatsapp_message(sender_number, f"🤔 Não encontrei a categoria '{category_name}'.")
            else:
                send_whatsapp_message(sender_number, "🤔 Não consegui identificar o nome da categoria para apagar.")

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

def check_and_send_reminders(db: Session):
    """Verifica lembretes pendentes (pontuais e recorrentes) e envia notificações."""
    now_utc = datetime.now(TZ_UTC)
    now_brt = now_utc.astimezone(TZ_SAO_PAULO)
    logging.info(f"CRON: Verificando lembretes em {now_utc.isoformat()}")

    # --- 1. Processar Lembretes Pontuais ---
    due_reminders = db.query(Reminder).filter(
        Reminder.is_recurring == False,
        Reminder.due_date <= now_utc,
        Reminder.is_sent == False
    ).all()

    for reminder in due_reminders:
        try:
            logging.info(f"Enviando lembrete PONTUAL para {reminder.user.phone_number}: {reminder.description}")
            due_time_brt = reminder.due_date.astimezone(TZ_SAO_PAULO).strftime('%H:%M')
            message = f"⏰ Lembrete: {reminder.description} às {due_time_brt}hrs."
            send_whatsapp_message(reminder.user.phone_number, message)
            reminder.is_sent = True
            db.commit()
        except Exception as e:
            logging.error(f"Falha ao enviar lembrete PONTUAL ID {reminder.id}: {e}")
            db.rollback()

    # --- 2. Processar Lembretes Recorrentes ---
    today_day = now_brt.day
    current_month = now_brt.month
    current_year = now_brt.year

    recurring_reminders_to_check = db.query(Reminder).filter(Reminder.is_recurring == True).all()

    for reminder in recurring_reminders_to_check:
        try:
            trigger_day = reminder.day_of_month - reminder.notification_day_offset
            if trigger_day < 1: trigger_day = 1
            
            if (today_day == trigger_day and 
               not (reminder.last_triggered_year == current_year and reminder.last_triggered_month == current_month)):

                logging.info(f"Enviando lembrete RECORRENTE para {reminder.user.phone_number}: {reminder.description}")
                message = (f"Olá! Passando para lembrar sobre sua conta de '{reminder.description}', "
                           f"que geralmente vence no dia {reminder.day_of_month}.\n\n"
                           f"Quando pagar, é só me responder com o valor (ex: *paguei {reminder.description.lower()} 152,80*) que eu registro pra você. 😉")
                
                send_whatsapp_message(reminder.user.phone_number, message)
                
                reminder.last_triggered_year = current_year
                reminder.last_triggered_month = current_month
                db.commit()
        except Exception as e:
            logging.error(f"Falha ao enviar lembrete RECORRENTE ID {reminder.id}: {e}")
            db.rollback()


# ==============================================================================
# ||                       APLICAÇÃO FASTAPI (ROTAS)                        ||
# ==============================================================================

app = FastAPI()

origins = [
    "http://localhost",
    "http://localhost:3000",
    "https://meu-gestor-dashboard.onrender.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"Status": "Meu Gestor Backend está online!"}

def run_check_reminders_task():
    db = SessionLocal()
    try:
        check_and_send_reminders(db)
    finally:
        db.close()

@app.get("/trigger/check-reminders/{secret_key}")
def trigger_reminders(secret_key: str, background_tasks: BackgroundTasks):
    if secret_key != CRON_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Chave secreta inválida.")
    
    background_tasks.add_task(run_check_reminders_task)
    return {"status": "success", "message": "Verificação de lembretes iniciada."}


@app.get("/api/verify-token/{token}")
def verify_token(token: str, db: Session = Depends(get_db)):
    token_obj = db.query(AuthToken).filter(AuthToken.token == token).first()
    if token_obj and token_obj.expires_at > datetime.now(TZ_UTC):
        phone_number = token_obj.user.phone_number.split('@')[0]
        db.delete(token_obj)
        db.commit()
        return {"phone_number": phone_number}
    if token_obj:
        db.delete(token_obj)
        db.commit()
    raise HTTPException(status_code=404, detail="Token inválido ou expirado.")

def get_user_from_query(db: Session, phone_number: str) -> User:
    if not phone_number:
        raise HTTPException(status_code=400, detail="Número de telefone é obrigatório.")
    
    cleaned_number = re.sub(r'\D', '', phone_number)
    if not cleaned_number.startswith('55'):
        cleaned_number = f"55{cleaned_number}"
    phone_number_jid = f"{cleaned_number}@s.whatsapp.net"
    
    user = db.query(User).filter(User.phone_number == phone_number_jid).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")
    return user

@app.get("/api/data/{phone_number}")
def get_user_data(phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    expenses = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.transaction_date.desc()).all()
    incomes = db.query(Income).filter(Income.user_id == user.id).order_by(Income.transaction_date.desc()).all()
    categories = get_user_categories(db, user)
    
    reminders = db.query(Reminder).filter(
        Reminder.user_id == user.id, 
        Reminder.is_recurring == False, 
        Reminder.is_sent == False
    ).order_by(Reminder.due_date.asc()).all()
    
    recurring_reminders = db.query(Reminder).filter(
        Reminder.user_id == user.id, 
        Reminder.is_recurring == True
    ).order_by(Reminder.day_of_month.asc()).all()
    
    expenses_data = [{"id": e.id, "description": e.description, "value": float(e.value), "category": e.category, "date": e.transaction_date.isoformat()} for e in expenses]
    incomes_data = [{"id": i.id, "description": i.description, "value": float(i.value), "date": i.transaction_date.isoformat()} for i in incomes]
    # CORRIGIDO: Adiciona verificação para evitar erro em datas nulas
    reminders_data = [{"id": r.id, "description": r.description, "due_date": r.due_date.isoformat()} for r in reminders if r.due_date]
    
    recurring_reminders_data = [{
        "id": r.id, 
        "description": r.description, 
        "day_of_month": r.day_of_month, 
        "notification_day_offset": r.notification_day_offset
    } for r in recurring_reminders]
    
    return {
        "user_id": user.id,
        "phone_number": user.phone_number,
        "expenses": expenses_data,
        "incomes": incomes_data,
        "categories": categories,
        "reminders": reminders_data,
        "recurring_reminders": recurring_reminders_data
    }

@app.put("/api/expense/{expense_id}")
def update_expense(expense_id: int, expense_data: ExpenseUpdate, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    expense = db.query(Expense).filter(Expense.id == expense_id, Expense.user_id == user.id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Despesa não encontrada.")
    
    expense.description = expense_data.description
    expense.value = expense_data.value
    expense.category = expense_data.category
    db.commit()
    db.refresh(expense)
    return expense

@app.delete("/api/expense/{expense_id}")
def delete_expense(expense_id: int, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    expense = db.query(Expense).filter(Expense.id == expense_id, Expense.user_id == user.id).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Despesa não encontrada.")
    
    db.delete(expense)
    db.commit()
    return {"status": "success", "message": "Despesa apagada."}

@app.put("/api/income/{income_id}")
def update_income(income_id: int, income_data: IncomeUpdate, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    income = db.query(Income).filter(Income.id == income_id, Income.user_id == user.id).first()
    if not income:
        raise HTTPException(status_code=404, detail="Crédito não encontrado.")
        
    income.description = income_data.description
    income.value = income_data.value
    db.commit()
    db.refresh(income)
    return income

@app.delete("/api/income/{income_id}")
def delete_income(income_id: int, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    income = db.query(Income).filter(Income.id == income_id, Income.user_id == user.id).first()
    if not income:
        raise HTTPException(status_code=404, detail="Crédito não encontrado.")
        
    db.delete(income)
    db.commit()
    return {"status": "success", "message": "Crédito apagado."}

@app.post("/api/categories/{phone_number}")
def add_category_api(phone_number: str, category: CategoryCreate, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    new_cat = create_user_category(db, user, category.name)
    return {"id": new_cat.id, "name": new_cat.name, "is_default": False}

@app.put("/api/category/{category_id}")
def update_category_api(category_id: int, category_data: CategoryUpdate, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    cat_to_update = db.query(Category).filter(Category.id == category_id, Category.user_id == user.id).first()
    if not cat_to_update:
        raise HTTPException(status_code=404, detail="Categoria não encontrada ou não pertence a este usuário.")
    cat_to_update.name = category_data.name
    db.commit()
    db.refresh(cat_to_update)
    return {"id": cat_to_update.id, "name": cat_to_update.name, "is_default": False}

@app.delete("/api/category/{category_id}")
def delete_category_api(category_id: int, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    cat_to_delete = db.query(Category).filter(Category.id == category_id, Category.user_id == user.id).first()
    if not cat_to_delete:
        raise HTTPException(status_code=404, detail="Categoria não encontrada ou não pertence a este usuário.")
    db.delete(cat_to_delete)
    db.commit()
    return {"status": "success", "message": "Categoria apagada."}

@app.put("/api/reminder/{reminder_id}")
def update_reminder_api(reminder_id: int, reminder_data: ReminderUpdate, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    reminder = db.query(Reminder).filter(Reminder.id == reminder_id, Reminder.user_id == user.id, Reminder.is_recurring == False).first()
    if not reminder:
        raise HTTPException(status_code=404, detail="Lembrete não encontrado.")
    
    reminder.description = reminder_data.description
    try:
        reminder.due_date = datetime.fromisoformat(reminder_data.due_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de data inválido. Use o formato ISO 8601.")
    
    db.commit()
    db.refresh(reminder)
    return {"id": reminder.id, "description": reminder.description, "due_date": reminder.due_date.isoformat()}

@app.delete("/api/reminder/{reminder_id}")
def delete_reminder_api(reminder_id: int, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    reminder = db.query(Reminder).filter(Reminder.id == reminder_id, Reminder.user_id == user.id, Reminder.is_recurring == False).first()
    if not reminder:
        raise HTTPException(status_code=404, detail="Lembrete não encontrado.")
    
    db.delete(reminder)
    db.commit()
    return {"status": "success", "message": "Lembrete apagado."}

@app.post("/api/recurring-reminders/{phone_number}")
def create_recurring_reminder_api(phone_number: str, reminder_data: RecurringReminderCreate, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    new_reminder = Reminder(
        description=reminder_data.description,
        day_of_month=reminder_data.day_of_month,
        notification_day_offset=reminder_data.notification_day_offset,
        is_recurring=True,
        user_id=user.id
    )
    db.add(new_reminder)
    db.commit()
    db.refresh(new_reminder)
    return {"id": new_reminder.id, "description": new_reminder.description, "day_of_month": new_reminder.day_of_month, "notification_day_offset": new_reminder.notification_day_offset}

@app.put("/api/recurring-reminder/{reminder_id}")
def update_recurring_reminder_api(reminder_id: int, reminder_data: RecurringReminderUpdate, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    reminder = db.query(Reminder).filter(Reminder.id == reminder_id, Reminder.user_id == user.id, Reminder.is_recurring == True).first()
    if not reminder:
        raise HTTPException(status_code=404, detail="Lembrete recorrente não encontrado.")
    
    reminder.description = reminder_data.description
    reminder.day_of_month = reminder_data.day_of_month
    reminder.notification_day_offset = reminder_data.notification_day_offset
    db.commit()
    db.refresh(reminder)
    return {"id": reminder.id, "description": reminder.description, "day_of_month": reminder.day_of_month, "notification_day_offset": reminder.notification_day_offset}

@app.delete("/api/recurring-reminder/{reminder_id}")
def delete_recurring_reminder_api(reminder_id: int, phone_number: str, db: Session = Depends(get_db)):
    user = get_user_from_query(db, phone_number)
    reminder = db.query(Reminder).filter(Reminder.id == reminder_id, Reminder.user_id == user.id, Reminder.is_recurring == True).first()
    if not reminder:
        raise HTTPException(status_code=404, detail="Lembrete recorrente não encontrado.")
    
    db.delete(reminder)
    db.commit()
    return {"status": "success", "message": "Lembrete recorrente apagado."}


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
        dify_result = process_text_message(message["conversation"], sender_number, db)
    elif "audioMessage" in message:
        dify_result = process_audio_message(message, sender_number, db)
    elif "imageMessage" in message:
        dify_result = process_image_message(message, sender_number)
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
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
