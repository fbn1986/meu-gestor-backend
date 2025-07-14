# ==============================================================================
# ||                   MEU GESTOR - BACKEND PRINCIPAL                     ||
# ==============================================================================
# Este arquivo contém toda a lógica para o assistente financeiro do WhatsApp.

# --- Importações de Bibliotecas ---
import logging
import json
import os
import re
from datetime import datetime, date, timedelta
from typing import List, Tuple # <<< NOVO: Para anotação de tipos

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

# (Toda a seção de configuração inicial continua a mesma)
load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY
DATABASE_URL = os.getenv("DATABASE_URL")
DIFY_API_URL = os.getenv("DIFY_API_URL")
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")
if FFMPEG_PATH and os.path.exists(FFMPEG_PATH): AudioSegment.converter = FFMPEG_PATH
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

# (As classes User e Expense continuam as mesmas)
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expenses = relationship("Expense", back_populates="user")

class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True, index=True)
    description = Column(String, nullable=False)
    value = Column(Numeric(10, 2), nullable=False)
    category = Column(String)
    transaction_date = Column(DateTime, default=datetime.utcnow)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="expenses")

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()


# ==============================================================================
# ||                   FUNÇÕES DE LÓGICA DE BANCO DE DADOS                    ||
# ==============================================================================

# (get_or_create_user, add_expense, delete_last_expense, edit_last_expense_value continuam iguais)
def get_or_create_user(db: Session, phone_number: str) -> User:
    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user: user = User(phone_number=phone_number); db.add(user); db.commit(); db.refresh(user)
    return user

def add_expense(db: Session, user: User, expense_data: dict):
    new_expense = Expense(description=expense_data.get("description"), value=expense_data.get("value"), category=expense_data.get("category"), user_id=user.id)
    db.add(new_expense); db.commit()

def delete_last_expense(db: Session, user: User) -> dict | None:
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    if last_expense:
        deleted_details = {"description": last_expense.description, "value": float(last_expense.value)}
        db.delete(last_expense); db.commit()
        return deleted_details
    return None

def edit_last_expense_value(db: Session, user: User, new_value: float) -> Expense | None:
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    if last_expense:
        last_expense.value = new_value; db.commit(); db.refresh(last_expense)
        return last_expense
    return None

# <<< INÍCIO DA FUNÇÃO DE RESUMO ALTERADA >>>
def get_expenses_summary(db: Session, user: User, period: str) -> Tuple[List[Expense], float] | None:
    """
    Busca a lista de despesas e o valor total para um determinado período.
    Retorna uma tupla (lista_de_despesas, total) ou None se o período não for reconhecido.
    """
    logging.info(f"Buscando resumo detalhado de despesas para o usuário {user.id} no período '{period}'")
    today = date.today()
    start_date = None
    period_lower = period.lower()

    if "mês" in period_lower: start_date = today.replace(day=1)
    elif "hoje" in period_lower: start_date = today
    elif "ontem" in period_lower:
        start_date = today - timedelta(days=1)
        end_date = today
        expenses = db.query(Expense).filter(
            Expense.user_id == user.id,
            Expense.transaction_date >= start_date,
            Expense.transaction_date < end_date
        ).order_by(Expense.transaction_date.asc()).all()
        total_value = sum(expense.value for expense in expenses)
        return expenses, total_value
    elif "7 dias" in period_lower: start_date = today - timedelta(days=7)
    elif "30 dias" in period_lower: start_date = today - timedelta(days=30)
    
    if start_date:
        expenses = db.query(Expense).filter(
            Expense.user_id == user.id,
            Expense.transaction_date >= start_date
        ).order_by(Expense.transaction_date.asc()).all()
        total_value = sum(expense.value for expense in expenses)
        return expenses, total_value
    
    return None # Período não reconhecido
# <<< FIM DA FUNÇÃO DE RESUMO ALTERADA >>>


# ==============================================================================
# ||                   FUNÇÕES DE COMUNICAÇÃO COM APIS EXTERNAS               ||
# ==============================================================================

# (As funções de comunicação com APIs externas continuam as mesmas)
def transcribe_audio(file_path: str) -> str | None:
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
    headers = {"Authorization": DIFY_API_KEY, "Content-Type": "application/json"}
    payload = {"inputs": {"query": text_query}, "query": text_query, "user": user_id, "response_mode": "blocking"}
    try:
        logging.info(f"Payload enviado ao Dify:\n{json.dumps(payload, indent=2)}")
        response = requests.post(f"{DIFY_API_URL}/chat-messages", headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        answer_str = response.json().get("answer", "")
        try: return json.loads(answer_str)
        except json.JSONDecodeError:
            logging.warning(f"Dify retornou texto puro: '{answer_str}'.")
            return {"action": "not_understood"}
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro na chamada à API do Dify: {e.response.text if e.response else e}")
        return None

def send_whatsapp_message(phone_number: str, message: str):
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

# (As funções process_text_message e process_audio_message continuam as mesmas)
def process_text_message(message_text: str, sender_number: str) -> dict | None:
    logging.info(f">>> PROCESSANDO TEXTO: [{sender_number}]")
    dify_user_id = re.sub(r'\D', '', sender_number)
    return call_dify_api(user_id=dify_user_id, text_query=message_text)

def process_audio_message(message: dict, sender_number: str) -> dict | None:
    logging.info(f">>> PROCESSANDO ÁUDIO de [{sender_number}]")
    media_url = message.get("url") or message.get("mediaUrl")
    if not media_url: logging.warning("Mensagem de áudio sem URL."); return None
    
    mp3_file_path = f"temp_audio_{sender_number}.mp3"
    ogg_path = f"temp_audio_{sender_number}.ogg"
    
    try:
        response = requests.get(media_url, timeout=30); response.raise_for_status()
        with open(ogg_path, "wb") as f: f.write(response.content)
        AudioSegment.from_ogg(ogg_path).export(mp3_path, format="mp3")
        
        transcribed_text = transcribe_audio(mp3_file_path)
        if not transcribed_text: return None
        
        dify_user_id = re.sub(r'\D', '', sender_number)
        return call_dify_api(user_id=dify_user_id, text_query=transcribed_text)
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)
        if os.path.exists(mp3_file_path): os.remove(mp3_file_path)

# <<< INÍCIO DA FUNÇÃO DE LÓGICA ALTERADA >>>
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

        elif action == "get_summary":
            period = dify_result.get("period", "período não identificado")
            summary_data = get_expenses_summary(db, user=user, period=period)
            
            if summary_data:
                expenses, total_spent = summary_data
                
                # Constrói a mensagem detalhada
                summary_message = f"📊 *Resumo para '{period}'*:\n\n"
                if expenses:
                    for expense in expenses:
                        # Formata a data para o padrão brasileiro
                        data_formatada = expense.transaction_date.strftime('%d/%m')
                        summary_message += f"*- {data_formatada}:* R$ {expense.value:.2f} - {expense.description}\n"
                    summary_message += f"\n*Total Gasto: R$ {total_spent:.2f}*"
                else:
                    summary_message += "Nenhuma despesa encontrada neste período."
                
                send_whatsapp_message(sender_number, summary_message)
            else:
                send_whatsapp_message(sender_number, f"Não consegui entender o período de tempo '{period}'. Tente 'hoje', 'ontem', ou 'este mês'.")

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
            fallback = "Não entendi. Tente de novo, por favor. Ex: 'gastei 50 no mercado', 'resumo do mês', 'apagar último gasto'."
            send_whatsapp_message(sender_number, fallback)

    except Exception as e:
        logging.error(f"Erro ao manusear a ação '{action}': {e}")
        send_whatsapp_message(sender_number, "❌ Ocorreu um erro interno ao processar seu pedido.")
# <<< FIM DA FUNÇÃO DE LÓGICA ALTERADA >>>


# ==============================================================================
# ||                          APLICAÇÃO FASTAPI (ROTAS)                         ||
# ==============================================================================

app = FastAPI()

@app.get("/")
def read_root(): return {"Status": "Meu Gestor Backend está online!"}

@app.post("/webhook/evolution")
async def evolution_webhook(request: Request, db: Session = Depends(get_db)):
    """Rota principal que recebe os webhooks da Evolution API."""
    data = await request.json()
    logging.info(f"DADOS RECEBIDOS: {json.dumps(data, indent=2)}")

    if data.get("event") != "messages.upsert": return {"status": "evento_ignorado"}
    message_data = data.get("data", {})
    if message_data.get("key", {}).get("fromMe"): return {"status": "mensagem_propria_ignorada"}
    
    sender_number = message_data.get("key", {}).get("remoteJid")
    message = message_data.get("message", {})
    if not sender_number or not message: return {"status": "dados_insuficientes"}

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)