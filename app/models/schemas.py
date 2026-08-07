from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Union

class PriceEntryCreate(BaseModel):
    product_id: Union[int, str] = Field(..., description="ID of the product")
    store_id: Union[int, str] = Field(..., description="ID of the store")
    harga: int = Field(..., description="Price value of the product at the store")
    sumber_user_id: Union[int, str] = Field(..., description="ID of the user submitting the price")

class PriceEntryOut(PriceEntryCreate):
    id: Union[int, str] = Field(..., description="ID of the price entry")
    timestamp: datetime = Field(..., description="Timestamp of when the price entry was created")
    status_verifikasi: str = Field(..., description="Verification status of the price entry (e.g., 'pending')")
