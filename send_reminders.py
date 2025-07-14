# send_reminders.py

import logging
import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, and_
from sqlalchemy.orm import Session

# Importa os modelos e a função de enviar mensagem do nosso arquivo principal
# Para isso funcionar, ambos os arquivos devem estar na mesma pasta
from main import User, Reminder, send_whatsapp_message, SessionLocal

# Configuração do logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [CronJob] - %(levelname)s - %(message)s')

def check_and_send_reminders():
    """
    Verifica o banco de dados por lembretes que estão no passado e ainda não foram enviados.
    """
    logging.info("Iniciando verificação de lembretes...")
    
    db: Session = SessionLocal()
    
    try:
        # Pega a hora atual
        now = datetime.utcnow()
        
        # Busca todos os lembretes que:
        # 1. Têm a data de vencimento no passado (ou seja, a hora já chegou)
        # 2. Ainda não foram marcados como enviados (is_sent == 'false')
        pending_reminders = db.query(Reminder).filter(
            and_(
                Reminder.due_date <= now,
                Reminder.is_sent == 'false'
            )
        ).all()
        
        if not pending_reminders:
            logging.info("Nenhum lembrete pendente encontrado.")
            return

        logging.info(f"Encontrados {len(pending_reminders)} lembretes para enviar.")

        for reminder in pending_reminders:
            try:
                # Busca o número de telefone do usuário associado ao lembrete
                user = db.query(User).filter(User.id == reminder.user_id).first()
                if user:
                    logging.info(f"Enviando lembrete '{reminder.description}' para {user.phone_number}")
                    
                    # Formata a mensagem de lembrete
                    reminder_message = f"⏰ *Lembrete:* {reminder.description}"
                    send_whatsapp_message(user.phone_number, reminder_message)
                    
                    # Marca o lembrete como enviado no banco de dados
                    reminder.is_sent = 'true'
                    db.commit()
                    logging.info(f"Lembrete ID {reminder.id} marcado como enviado.")
                else:
                    logging.warning(f"Usuário não encontrado para o lembrete ID {reminder.id}. Marcando como enviado para evitar repetição.")
                    reminder.is_sent = 'true'
                    db.commit()

            except Exception as e:
                logging.error(f"Erro ao processar o lembrete ID {reminder.id}: {e}")
                # Mesmo se falhar, marcamos como enviado para não sobrecarregar o usuário com o mesmo lembrete
                reminder.is_sent = 'true'
                db.commit()

    finally:
        db.close()
        logging.info("Verificação de lembretes concluída.")

if __name__ == "__main__":
    # Carrega as variáveis de ambiente para o script poder acessar as chaves de API
    load_dotenv()
    check_and_send_reminders()