from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task
def send_contract_email(contract_id: str, email: str):
    logger.info(f"Placeholder contract email task for contract={contract_id} email={email}")


@shared_task
def generate_contract_pdf(contract_id: str, template_id: str = None):
    logger.info(f"Placeholder contract PDF generation for contract={contract_id}")


@shared_task
def check_expired_contracts():
    logger.info("Placeholder expired contract check")


@shared_task
def send_contract_reminder(contract_id: str):
    logger.info(f"Placeholder reminder for contract={contract_id}")
