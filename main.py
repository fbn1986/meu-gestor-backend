# main.py (Versão com Edição, Remoção e Resumo)

# --- Importações, Configurações, Modelos, etc. ---
# (Todo o início do código continua o mesmo)
from fastapi import FastAPI, Request, Depends
from sqlalchemy.orm import Session
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
load_dotenv()
import openai
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY
DATABASE_URL = os.getenv("DATABASE_URL")
DIFY_API_URL = os.getenv("DIFY_API_URL")
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
if FFMPEG_PATH and os.path.exists(FFMPEG_PATH): AudioSegment.converter = FFMPEG_PATH
try:
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base()
except Exception as e: logging.error(f"Erro fatal ao conectar ao banco de dados: {e}"); exit()
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
    db = SessionLocal();
    try: yield db
    finally: db.close()

# --- Funções Auxiliares ---
def get_or_create_user(db: Session, phone_number: str):
    user = db.query(User).filter(User.phone_number == phone_number).first()
    if not user: user = User(phone_number=phone_number); db.add(user); db.commit(); db.refresh(user)
    return user

def add_expense(db: Session, user: User, expense_data: dict):
    new_expense = Expense(description=expense_data.get("description"), value=expense_data.get("value"), category=expense_data.get("category"), user_id=user.id)
    db.add(new_expense); db.commit()

def get_expenses_summary(db: Session, user: User, period: str):
    today = date.today(); start_date = None; period_lower = period.lower()
    if "mês" in period_lower: start_date = today.replace(day=1)
    elif "hoje" in period_lower: start_date = today
    elif "ontem" in period_lower:
        start_date = today - timedelta(days=1); end_date = today 
        total_value = db.query(func.sum(Expense.value)).filter(Expense.user_id == user.id, Expense.transaction_date >= start_date, Expense.transaction_date < end_date).scalar()
        return total_value or 0.0
    elif "7 dias" in period_lower: start_date = today - timedelta(days=7)
    elif "30 dias" in period_lower: start_date = today - timedelta(days=30)
    if start_date:
        total_value = db.query(func.sum(Expense.value)).filter(Expense.user_id == user.id, Expense.transaction_date >= start_date).scalar()
        return total_value or 0.0
    return None

def delete_last_expense(db: Session, user: User):
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    if last_expense:
        deleted_details = {"description": last_expense.description, "value": float(last_expense.value)}
        db.delete(last_expense); db.commit()
        return deleted_details
    return None

# <<< INÍCIO DA NOVA FUNÇÃO DE EDITAR >>>
def edit_last_expense_value(db: Session, user: User, new_value: float):
    """Encontra e edita o valor da última despesa registrada por um usuário."""
    logging.info(f"Tentando editar o valor da última despesa do usuário {user.id} para {new_value}")
    
    last_expense = db.query(Expense).filter(Expense.user_id == user.id).order_by(Expense.id.desc()).first()
    
    if last_expense:
        logging.info(f"Encontrada despesa para editar: ID {last_expense.id}, Valor antigo: {last_expense.value}")
        last_expense.value = new_value
        db.commit()
        db.refresh(last_expense) # Atualiza o objeto com os novos dados do DB
        logging.info("Valor da despesa editado com sucesso.")
        return last_expense
    else:
        logging.warning("Nenhuma despesa encontrada para este usuário.")
        return None
# <<< FIM DA NOVA FUNÇÃO DE EDITAR >>>

# --- Áudio, Dify, WhatsApp e Processadores (sem alterações) ---
def download_and_convert_audio(media_url: str):
    ogg_path = "temp_audio.ogg"; mp3_path = "temp_audio.mp3"
    try:
        response = requests.get(media_url, timeout=30); response.raise_for_status()
        with open(ogg_path, "wb") as f: f.write(response.content)
        AudioSegment.from_ogg(ogg_path).export(mp3_path, format="mp3"); return mp3_path
    except Exception as e: logging.error(f"Erro ao processar áudio: {e}"); return None
    finally:
        if os.path.exists(ogg_path): os.remove(ogg_path)

# <<< VERSÃO CORRIGIDA E MAIS ROBUSTA >>>
def call_dify_api(user_id: str, text_query: str = None):
    """Envia a mensagem para o Dify e lida com respostas que não são JSON."""
    headers = {"Authorization": DIFY_API_KEY, "Content-Type": "application/json"}
    payload = {
        "inputs": {"query": text_query} if text_query else {},
        "query": text_query or "Analisar despesa",
        "user": user_id,
        "response_mode": "blocking"
    }
    
    try:
        logging.info(f"Payload enviado ao Dify:\n{json.dumps(payload, indent=2)}")
        response = requests.post(f"{DIFY_API_URL}/chat-messages", headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        
        response_data = response.json()
        answer_str = response_data.get("answer", "") # Pega a resposta, ou uma string vazia se não houver

        # Tenta decodificar a resposta como JSON
        try:
            parsed_json = json.loads(answer_str)
            return parsed_json
        except json.JSONDecodeError:
            # Se falhar (resposta era texto puro ou vazia), nós mesmos criamos a resposta padrão
            logging.warning(f"Dify retornou texto puro em vez de JSON: '{answer_str}'. Tratando como 'not_understood'.")
            return {"action": "not_understood"}

    except requests.exceptions.RequestException as e:
        logging.error(f"Erro na chamada ao Dify: {e}")
        if e.response:
            logging.error(f"Resposta da API: {e.response.text}")
        return None # Retorna None em caso de falha de conexão
    except Exception as e:
        logging.error(f"Um erro inesperado ocorreu na função call_dify_api: {e}")
        return None

# --- FastAPI App ---
app = FastAPI()
@app.get("/")
def read_root(): return {"Status": "Meu Gestor Backend está online!"}


@app.post("/webhook/evolution")
async def evolution_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
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
    else: return {"status": "tipo_nao_suportado"}

    if not dify_result: return {"status": "falha_processamento"}
    
    action = dify_result.get("action")
    user = get_or_create_user(db, phone_number=sender_number)

    # <<< INÍCIO DO BLOCO LÓGICO ALTERADO >>>
    if action == "register_expense":
        try:
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
            period = dify_result.get("period", "período não identificado")
            total_spent = get_expenses_summary(db, user=user, period=period)
            if total_spent is not None:
                formatted_total = f"{total_spent:.2f}".replace('.', ',')
                summary_message = f"📊 Resumo para '{period}':\n\nVocê gastou um total de *R$ {formatted_total}*."
                send_whatsapp_message(sender_number, summary_message)
            else:
                send_whatsapp_message(sender_number, f"Não consegui entender o período de tempo '{period}'. Tente 'hoje', 'ontem', ou 'este mês'.")
        except Exception as e:
            logging.error(f"Erro ao gerar resumo: {e}")
            send_whatsapp_message(sender_number, "❌ Tive um problema para gerar seu resumo.")

    elif action == "delete_last_expense":
        try:
            deleted_expense = delete_last_expense(db, user=user)
            if deleted_expense:
                valor = deleted_expense.get('value', 0)
                descricao = deleted_expense.get('description', 'N/A')
                formatted_valor = f"{valor:.2f}".replace('.', ',')
                confirmation = f"🗑️ Despesa anterior ('{descricao}' de R$ {formatted_valor}) foi removida com sucesso."
                send_whatsapp_message(sender_number, confirmation)
            else:
                send_whatsapp_message(sender_number, "🤔 Não encontrei nenhuma despesa para apagar.")
        except Exception as e:
            logging.error(f"Erro ao apagar despesa: {e}")
            send_whatsapp_message(sender_number, "❌ Tive um problema para apagar sua última despesa.")

    elif action == "edit_last_expense_value":
        try:
            new_value = float(dify_result.get("new_value", 0))
            updated_expense = edit_last_expense_value(db, user=user, new_value=new_value)
            if updated_expense:
                descricao = updated_expense.description
                valor_corrigido = f"{updated_expense.value:.2f}".replace('.',',')
                confirmation = f"✏️ Valor da despesa '{descricao}' corrigido para *R$ {valor_corrigido}*."
                send_whatsapp_message(sender_number, confirmation)
            else:
                send_whatsapp_message(sender_number, "🤔 Não encontrei nenhuma despesa para editar.")
        except Exception as e:
            logging.error(f"Erro ao editar despesa: {e}")
            send_whatsapp_message(sender_number, "❌ Tive um problema para editar sua última despesa.")

    else: # Inclui a ação "not_understood" ou qualquer outra não tratada
        fallback = "Não consegui entender. Tente registrar um gasto (ex: 'gastei 20 no almoço'), pedir um resumo ou apagar o último gasto."
        send_whatsapp_message(sender_number, fallback)
    # <<< FIM DO BLOCO LÓGICO ALTERADO >>>

    return {"status": "processado"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)