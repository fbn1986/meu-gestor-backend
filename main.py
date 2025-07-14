# main.py (Versão Final com Resumo Avançado)

# --- Importações ---
from fastapi import FastAPI, Request, Depends
from sqlalchemy.orm import Session
# <<< ALTERADO: Adicionado 'timedelta' para cálculos com dias >>>
from datetime import datetime, date, timedelta
import logging
import requests
import json
import os
import re

from dotenv import load_dotenv
from pydub import AudioSegment
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, func, and_
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.exc import SQLAlchemyError

# --- Carrega variáveis do .env ---
load_dotenv()
logging.info("Variáveis de ambiente carregadas.")

# --- OpenAI Whisper ---
import openai
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# --- Configurações do ambiente ---
DATABASE_URL = os.getenv("DATABASE_URL")
DIFY_API_URL = os.getenv("DIFY_API_URL")
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")

# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- FFmpeg ---
if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
    AudioSegment.converter = FFMPEG_PATH
    logging.info(f"Pydub configurado para usar FFmpeg em: {FFMPEG_PATH}")
else:
    logging.warning("Caminho para FFMPEG_PATH não encontrado ou inválido.")

# --- Banco de Dados e Modelos ---
try:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()
    logging.info("Conexão com o banco de dados estabelecida com sucesso.")
except Exception as e:
    logging.error(f"Erro ao conectar no banco de dados: {e}")
    exit()

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
    try:
        yield db
    finally:
        db.close()

# --- Funções Auxiliares ---
def get_or_create_user(db: Session, phone_number: str):
    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user:
        user = User(phone_number=phone_number)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user

def add_expense(db: Session, user: User, expense_data: dict):
    new_expense = Expense(
        description=expense_data.get("description"),
        value=expense_data.get("value"),
        category=expense_data.get("category"),
        user_id=user.id
    )
    db.add(new_expense)
    db.commit()

# <<< INÍCIO DA FUNÇÃO DE RESUMO ALTERADA >>>
def get_expenses_summary(db: Session, user: User, period: str):
    """Busca despesas e retorna o valor total para um determinado período."""
    logging.info(f"Buscando resumo de despesas para o usuário {user.id} no período '{period}'")
    
    today = date.today()
    start_date = None # Vamos definir a data de início baseada no período

    # Converte o período para minúsculas para facilitar a comparação
    period_lower = period.lower()

    if "mês" in period_lower:
        start_date = today.replace(day=1)
    elif "hoje" in period_lower:
        start_date = today
    elif "ontem" in period_lower:
        # Para "ontem", o ideal é pegar o dia inteiro de ontem
        start_date = today - timedelta(days=1)
        end_date = today 
        total_value = db.query(func.sum(Expense.value)).filter(
            Expense.user_id == user.id,
            Expense.transaction_date >= start_date,
            Expense.transaction_date < end_date # Menor que hoje para pegar só ontem
        ).scalar()
        return total_value or 0.0
    elif "7 dias" in period_lower:
        start_date = today - timedelta(days=7)
    elif "30 dias" in period_lower:
        start_date = today - timedelta(days=30)
    
    # Se uma data de início foi definida, fazemos a busca
    if start_date:
        total_value = db.query(func.sum(Expense.value)).filter(
            Expense.user_id == user.id,
            Expense.transaction_date >= start_date
        ).scalar()
        return total_value or 0.0

    # Se o período não for reconhecido, retorna None para ser tratado no webhook
    return None
# <<< FIM DA FUNÇÃO DE RESUMO ALTERADA >>>


# --- Áudio, Dify, WhatsApp e Processadores (sem alterações) ---
def download_and_convert_audio(media_url: str):
    ogg_path = "temp_audio.ogg"
    mp3_path = "temp_audio.mp3"
    try:
        response = requests.get(media_url, timeout=30)
        response.raise_for_status()
        with open(ogg_path, "wb") as f:
            f.write(response.content)
        audio = AudioSegment.from_ogg(ogg_path)
        audio.export(mp3_path, format="mp3")
        return mp3_path
    except Exception as e:
        logging.error(f"Erro ao processar áudio: {e}")
        return None
    finally:
        if os.path.exists(ogg_path):
            os.remove(ogg_path)

def call_dify_api(user_id: str, text_query: str = None):
    headers = {"Authorization": DIFY_API_KEY, "Content-Type": "application/json"}
    payload = {
        "inputs": {"query": text_query} if text_query else {},
        "query": text_query or "Analisar despesa do áudio",
        "user": user_id,
        "response_mode": "blocking"
    }
    logging.info(f"Payload enviado ao Dify:\n{json.dumps(payload, indent=2)}")
    try:
        response = requests.post(f"{DIFY_API_URL}/chat-messages", headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        response_data = response.json()
        answer_str = response_data.get("answer", "{}")
        return json.loads(answer_str)
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro na chamada ao Dify: {e}")
        if e.response:
            logging.error(f"Resposta da API: {e.response.text}")
        return None
    except json.JSONDecodeError:
        logging.error("Resposta da Dify não era JSON válido.")
        return None

def send_whatsapp_message(phone_number: str, message: str):
    url = f"{EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    clean_number = phone_number.split('@')[0]
    payload = { "number": clean_number, "options": {"delay": 1200}, "textMessage": {"text": message} }
    print("\n=== PAYLOAD ENVIADO AO EVOLUTION ===")
    print("URL:", url); print("HEADERS:", headers); print("PAYLOAD:", json.dumps(payload, indent=2))
    try:
        response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=30)
        print("STATUS CODE:", response.status_code); print("RESPOSTA:", response.text)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro ao enviar mensagem via WhatsApp: {e.response.text if e.response else e}")

def process_text_message(message_text: str, sender_number: str, db: Session):
    logging.info(f">>> PROCESSANDO TEXTO: [{sender_number}] {message_text}")
    dify_user_id = re.sub(r'\D', '', sender_number)
    return call_dify_api(user_id=dify_user_id, text_query=message_text)

def process_audio_message(message: dict, sender_number: str, db: Session):
    logging.info(f">>> PROCESSANDO ÁUDIO de [{sender_number}]")
    media_url = message.get("url") or message.get("mediaUrl")
    if not media_url:
        logging.warning("Mensagem de áudio sem URL."); return None
    mp3_file_path = download_and_convert_audio(media_url)
    if not mp3_file_path: return None
    try:
        with open(mp3_file_path, "rb") as audio_file:
            transcription = openai.Audio.transcribe("whisper-1", audio_file)
            text = transcription["text"]
            logging.info(f"Transcrição: {text}")
    except Exception as e:
        logging.error(f"Erro na transcrição com Whisper: {e}"); return None
    finally:
        if os.path.exists(mp3_file_path):
            os.remove(mp3_file_path)
    dify_user_id = re.sub(r'\D', '', sender_number)
    return call_dify_api(user_id=dify_user_id, text_query=text)

# --- FastAPI App ---
app = FastAPI()

@app.get("/")
def read_root():
    return {"Status": "Meu Gestor Backend está online!"}

# <<< ALTERADO: Pequeno ajuste na lógica do webhook para tratar período não reconhecido >>>
@app.post("/webhook/evolution")
async def evolution_webhook(request: Request, db: Session = Depends(get_db)):
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
        dify_result = process_text_message(message["conversation"], sender_number, db)
    elif "audioMessage" in message:
        dify_result = process_audio_message(message, sender_number, db)
    else:
        logging.info(f"Tipo de mensagem não suportado: {list(message.keys())}"); return {"status": "tipo_nao_suportado"}

    if not dify_result:
        logging.warning("Sem resultado do Dify."); return {"status": "falha_processamento"}

    logging.info(f"Resposta do Dify: {json.dumps(dify_result, indent=2)}")
    action = dify_result.get("action")

    if action == "register_expense":
        try:
            user = get_or_create_user(db, phone_number=sender_number)
            add_expense(db, user=user, expense_data=dify_result)
            valor = float(dify_result.get('value', 0))
            descricao = dify_result.get('description', 'N/A')
            confirmation = f"✅ Despesa de R$ {valor:.2f} ({descricao}) registrada com sucesso!"
            send_whatsapp_message(sender_number, confirmation)
        except Exception as e:
            logging.error(f"Erro ao registrar despesa: {e}")
            send_whatsapp_message(sender_number, "❌ Tive um problema para guardar sua despesa.")

    elif action == "get_summary":
        try:
            user = get_or_create_user(db, phone_number=sender_number)
            period = dify_result.get("period", "período não identificado")
            total_spent = get_expenses_summary(db, user=user, period=period)
            
            if total_spent is not None:
                formatted_total = f"{total_spent:.2f}".replace('.', ',')
                summary_message = f"📊 Resumo para '{period}':\n\nVocê gastou um total de *R$ {formatted_total}*."
                send_whatsapp_message(sender_number, summary_message)
            else:
                # Caso a função retorne None (período não reconhecido)
                send_whatsapp_message(sender_number, f"Não consegui entender o período de tempo '{period}'. Tente 'hoje', 'ontem', 'este mês' ou 'últimos 7 dias'.")
        except Exception as e:
            logging.error(f"Erro ao gerar resumo: {e}")
            send_whatsapp_message(sender_number, "❌ Tive um problema para gerar seu resumo.")

    else:
        fallback = "Não consegui entender. Você pode tentar dizer 'gastei 20 reais no almoço' ou 'qual meu resumo do mês?'"
        send_whatsapp_message(sender_number, fallback)

    return {"status": "processado"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)