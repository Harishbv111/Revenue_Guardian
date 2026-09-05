from sqlalchemy import Column, Integer, String, Float
from database import Base

class RecoveryCase(Base):
    __tablename__ = "recovery_cases"
    id = Column(Integer, primary_key=True, index=True)
    case_id = Column(String, unique=True, index=True, nullable=False)
    payment_id = Column(String, unique=True, index=True, nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, default="INR")
    failure_reason = Column(String, nullable=True)
    recovery_probability = Column(Float, nullable=False)
    expected_recovery = Column(Float, nullable=False)
    priority = Column(String, nullable=False)
    status = Column(String, default="PENDING")
    recommended_action = Column(String, nullable=True)
    action_reason = Column(String, nullable=True)
    customer_message = Column(String, nullable=True)
    action_triggered_at = Column(String, nullable=True)
    recovered_at = Column(String, nullable=True)
    recovered_amount = Column(Float, nullable=True)
    recovery_result = Column(String, nullable=True)
    payment_link = Column(String, nullable=True)
    payment_link_id = Column(String, nullable=True)
