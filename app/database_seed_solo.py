import os
import sys
import uuid
import logging
from dotenv import load_dotenv

# Ensure backend directory is in python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, engine
from app.models.db_models import Store

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rakoon_backend.seed")

# Load environment variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path=env_path, override=True)

# Solo area stores datasets (coordinates around -7.56, 110.82)
SOLO_STORES = [
    {
        "nama": "Indomaret Slamet Riyadi",
        "alamat": "Jl. Slamet Riyadi No. 150, Surakarta",
        "lat": -7.5642,
        "lng": 110.8123
    },
    {
        "nama": "Alfamart Adi Sucipto",
        "alamat": "Jl. Adi Sucipto No. 45, Surakarta",
        "lat": -7.5531,
        "lng": 110.7984
    },
    {
        "nama": "Luwes Kestalan swalayan",
        "alamat": "Jl. Letjen S. Parman No. 22, Surakarta",
        "lat": -7.5598,
        "lng": 110.8245
    },
    {
        "nama": "Toko Kelontong Pak Bambang",
        "alamat": "Jl. Gajah Mada No. 89, Surakarta",
        "lat": -7.5621,
        "lng": 110.8189
    },
    {
        "nama": "Sami Luwes Swalayan",
        "alamat": "Jl. Mayor Sunaryo No. 4, Surakarta",
        "lat": -7.5712,
        "lng": 110.8288
    },
    {
        "nama": "Super Indo Adisucipto Solo",
        "alamat": "Jl. Adisucipto No. 115, Surakarta",
        "lat": -7.5512,
        "lng": 110.7895
    },
    {
        "nama": "Indomaret Purwosari",
        "alamat": "Jl. Slamet Riyadi No. 360, Surakarta",
        "lat": -7.5678,
        "lng": 110.8012
    }
]

def seed_stores():
    db = SessionLocal()
    inserted_count = 0
    skipped_count = 0
    
    logger.info("Starting to seed Solo area stores...")
    
    try:
        for store_data in SOLO_STORES:
            # Check if store already exists with the same name and near coordinates (idempotency check)
            # We check the exact combination of nama, lat, and lng
            existing_store = db.query(Store).filter(
                Store.nama == store_data["nama"],
                Store.lat == store_data["lat"],
                Store.lng == store_data["lng"]
            ).first()
            
            if existing_store:
                logger.info(f"Store already exists, skipping: {store_data['nama']} (ID: {existing_store.id})")
                skipped_count += 1
                continue
            
            # Create new store
            new_store = Store(
                id=str(uuid.uuid4()),
                nama=store_data["nama"],
                alamat=store_data["alamat"],
                lat=store_data["lat"],
                lng=store_data["lng"]
            )
            db.add(new_store)
            logger.info(f"Adding store to session: {store_data['nama']}")
            inserted_count += 1
            
        if inserted_count > 0:
            db.commit()
            logger.info(f"Successfully seeded {inserted_count} Solo stores in the database.")
        else:
            logger.info("No new stores needed to be inserted.")
            
        logger.info(f"Summary: {inserted_count} inserted, {skipped_count} skipped.")
        
    except Exception as e:
        db.rollback()
        logger.error(f"Error seeding database stores: {str(e)}")
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    seed_stores()
