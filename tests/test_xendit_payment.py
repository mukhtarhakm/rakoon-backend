import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, get_current_admin_user
from app.models.db_models import Store, StoreOwner, AdCampaign, PaymentTransaction
from app.services.xendit_service import XenditAPIError

SQLALCHEMY_DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


TEST_USER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_USER_ID = "22222222-2222-2222-2222-222222222222"
SANDBOX_KEY = "xnd_development_test_key_abc123"
CALLBACK_TOKEN = "test_webhook_callback_token_xyz789"


class TestXenditPaymentIntegration(unittest.TestCase):

    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = TestingSessionLocal()
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = lambda: TEST_USER_ID
        app.dependency_overrides[get_current_admin_user] = lambda: {"user_id": "admin"}
        self.client = TestClient(app)

        # Set sandbox environment variables
        self.old_env = {
            "XENDIT_SECRET_KEY": os.environ.get("XENDIT_SECRET_KEY"),
            "XENDIT_WEBHOOK_TOKEN": os.environ.get("XENDIT_WEBHOOK_TOKEN"),
            "XENDIT_ENVIRONMENT": os.environ.get("XENDIT_ENVIRONMENT"),
        }
        os.environ["XENDIT_SECRET_KEY"] = SANDBOX_KEY
        os.environ["XENDIT_WEBHOOK_TOKEN"] = CALLBACK_TOKEN
        os.environ["XENDIT_ENVIRONMENT"] = "sandbox"

        # Seed verified store and owner
        self.store = Store(
            nama="Superindo Kaliurang",
            alamat="Jl. Kaliurang KM 6, Sleman, Yogyakarta",
            lat=-7.7550,
            lng=110.3800
        )
        self.db.add(self.store)
        self.db.commit()
        self.db.refresh(self.store)

        self.owner = StoreOwner(
            user_id=TEST_USER_ID,
            store_id=self.store.id,
            status="verified",
            verified_at=datetime.now(timezone.utc),
        )
        self.db.add(self.owner)
        self.db.commit()

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.close()
        Base.metadata.drop_all(bind=engine)

        for k, v in self.old_env.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def _create_pending_campaign(self, duration_days=3):
        create_res = self.client.post("/ads/campaigns", json={
            "store_id": str(self.store.id),
            "title": "Promo Akhir Pekan Superindo",
            "banner_url": "https://example.com/superindo.jpg",
            "duration_days": duration_days,
        })
        self.assertEqual(create_res.status_code, 201)
        return create_res.json()

    # 1. Merchant owner requests checkout successfully
    @patch("app.services.xendit_service.create_invoice")
    def test_01_merchant_owner_checkout_success(self, mock_create_inv):
        campaign_data = self._create_pending_campaign(duration_days=3)
        campaign_id = campaign_data["id"]

        mock_create_inv.return_value = {
            "id": "xinv_test_12345",
            "external_id": campaign_data["payment_ref"],
            "status": "PENDING",
            "invoice_url": "https://checkout-staging.xendit.co/v2/invoice-12345",
            "amount": 15000,
            "expiry_date": "2026-10-11T12:00:00.000Z",
        }

        pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(pay_res.status_code, 200)
        pay_data = pay_res.json()

        self.assertEqual(pay_data["campaign_id"], campaign_id)
        self.assertEqual(pay_data["status"], "PENDING")
        self.assertEqual(pay_data["amount"], 15000)
        self.assertEqual(pay_data["invoice_url"], "https://checkout-staging.xendit.co/v2/invoice-12345")
        self.assertEqual(pay_data["xendit_invoice_id"], "xinv_test_12345")

        # Campaign in DB MUST remain pending_payment and unpaid
        campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        self.assertEqual(campaign.status, "pending_payment")
        self.assertEqual(campaign.payment_status, "unpaid")

        # Transaction record created
        tx = self.db.query(PaymentTransaction).filter(PaymentTransaction.campaign_id == campaign_id).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.status, "PENDING")
        self.assertEqual(tx.provider, "xendit")
        self.assertEqual(tx.environment, "sandbox")

    # 2. Other user cannot request checkout for another merchant's campaign
    def test_02_other_user_cannot_checkout_merchant_campaign(self):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]

        # Switch to unauthorized user
        app.dependency_overrides[get_current_user] = lambda: OTHER_USER_ID
        pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(pay_res.status_code, 403)
        self.assertIn("tidak memiliki izin", pay_res.json()["detail"])

    # 3. Client manipulated amount is ignored
    @patch("app.services.xendit_service.create_invoice")
    def test_03_client_manipulated_amount_is_ignored(self, mock_create_inv):
        campaign_data = self._create_pending_campaign(duration_days=7) # 30000
        campaign_id = campaign_data["id"]

        mock_create_inv.return_value = {
            "id": "xinv_7days",
            "external_id": campaign_data["payment_ref"],
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-7",
            "amount": 30000,
        }

        # Send custom payload attempting to pay Rp 100
        pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay", json={"amount": 100})
        self.assertEqual(pay_res.status_code, 200)

        # Server used server pricing (30000), not client injected amount
        mock_create_inv.assert_called_once()
        called_amount = mock_create_inv.call_args[1]["amount"]
        self.assertEqual(called_amount, 30000)

    # 4. Already paid or active campaign rejects checkout
    def test_04_already_paid_or_active_campaign_rejected(self):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]

        # Mark campaign as paid in DB
        campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        campaign.payment_status = "paid"
        campaign.status = "active"
        self.db.commit()

        pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(pay_res.status_code, 400)
        self.assertIn("sudah lunas", pay_res.json()["detail"])

    # 5. Missing Xendit configuration fails safely
    def test_05_missing_xendit_config_fails_safely(self):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]

        os.environ.pop("XENDIT_SECRET_KEY", None)
        pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(pay_res.status_code, 503)
        self.assertIn("belum dikonfigurasi", pay_res.json()["detail"])

    # 6. Provider error does not activate campaign
    @patch("app.services.xendit_service.create_invoice")
    def test_06_provider_error_does_not_activate_campaign(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]

        mock_create_inv.side_effect = XenditAPIError("Xendit network timeout")

        pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(pay_res.status_code, 502)

        # Campaign must remain pending_payment and unpaid
        campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        self.assertEqual(campaign.status, "pending_payment")
        self.assertEqual(campaign.payment_status, "unpaid")

    # 7. Multiple checkout clicks are idempotent
    @patch("app.services.xendit_service.create_invoice")
    def test_07_idempotent_checkout_avoids_duplicate_invoice(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]

        mock_create_inv.return_value = {
            "id": "xinv_single_call",
            "external_id": campaign_data["payment_ref"],
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-single",
            "amount": 15000,
        }

        # First checkout call
        res1 = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(res1.status_code, 200)

        # Second checkout call (user double clicked)
        res2 = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["invoice_url"], "https://checkout.xendit.co/inv-single")

        # Xendit API was called only ONCE!
        self.assertEqual(mock_create_inv.call_count, 1)

    # 8. Webhook with correct token processes valid transaction
    @patch("app.services.xendit_service.create_invoice")
    def test_08_webhook_valid_token_and_data_activates_campaign(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_webhook_test",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-wh",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        # Send valid Xendit webhook callback
        wh_payload = {
            "id": "xinv_webhook_test",
            "external_id": ext_id,
            "status": "PAID",
            "paid_amount": 15000,
            "currency": "IDR",
            "paid_at": "2026-10-10T12:30:00.000Z",
            "payment_method": "QRIS",
        }
        wh_res = self.client.post(
            "/ads/webhook/xendit",
            json=wh_payload,
            headers={"x-callback-token": CALLBACK_TOKEN}
        )
        self.assertEqual(wh_res.status_code, 200)
        self.assertEqual(wh_res.json()["status"], "success")

        # Verify DB changes
        campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        self.assertEqual(campaign.status, "active")
        self.assertEqual(campaign.payment_status, "paid")
        self.assertEqual(campaign.payment_ref, "xinv_webhook_test")
        self.assertIsNotNone(campaign.start_at)
        self.assertIsNotNone(campaign.expires_at)

        tx = self.db.query(PaymentTransaction).filter(PaymentTransaction.external_id == ext_id).first()
        self.assertEqual(tx.status, "PAID")
        self.assertIsNotNone(tx.paid_at)

    # 9. Webhook with invalid or missing token is rejected
    def test_09_webhook_invalid_or_missing_token_rejected(self):
        wh_payload = {
            "id": "xinv_fake",
            "external_id": "INV-20261010-FAKE",
            "status": "PAID",
            "paid_amount": 15000,
        }

        # Missing token header
        no_token_res = self.client.post("/ads/webhook/xendit", json=wh_payload)
        self.assertEqual(no_token_res.status_code, 401)

        # Invalid token header
        bad_token_res = self.client.post(
            "/ads/webhook/xendit",
            json=wh_payload,
            headers={"x-callback-token": "wrong_token_12345"}
        )
        self.assertEqual(bad_token_res.status_code, 401)

    # 10. Webhook with unknown external_id is rejected
    def test_10_webhook_unknown_external_id_rejected(self):
        wh_payload = {
            "id": "xinv_unknown",
            "external_id": "INV-NONEXISTENT-9999",
            "status": "PAID",
            "paid_amount": 15000,
            "currency": "IDR",
        }
        wh_res = self.client.post(
            "/ads/webhook/xendit",
            json=wh_payload,
            headers={"x-callback-token": CALLBACK_TOKEN}
        )
        self.assertEqual(wh_res.status_code, 404)

    # 11. Webhook with mismatched amount or currency does not activate campaign
    @patch("app.services.xendit_service.create_invoice")
    def test_11_webhook_mismatched_amount_or_currency_rejected(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_mismatch",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-mismatch",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        # Underpayment (Rp 5000 instead of 15000)
        res_underpay = self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_mismatch", "external_id": ext_id, "status": "PAID", "paid_amount": 5000, "currency": "IDR"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )
        self.assertEqual(res_underpay.status_code, 400)
        self.assertIn("Amount mismatch", res_underpay.json()["detail"])

        # Currency mismatch (USD instead of IDR)
        res_curr = self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_mismatch", "external_id": ext_id, "status": "PAID", "paid_amount": 15000, "currency": "USD"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )
        self.assertEqual(res_curr.status_code, 400)
        self.assertIn("Currency mismatch", res_curr.json()["detail"])

        # Campaign remains pending_payment and unpaid
        campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        self.assertEqual(campaign.status, "pending_payment")
        self.assertEqual(campaign.payment_status, "unpaid")

    # 12. Webhook with non-final status does not activate campaign
    @patch("app.services.xendit_service.create_invoice")
    def test_12_webhook_non_final_status_does_not_activate(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_pending_wh",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-p",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        wh_res = self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_pending_wh", "external_id": ext_id, "status": "PENDING", "paid_amount": 15000, "currency": "IDR"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )
        self.assertEqual(wh_res.status_code, 200)
        self.assertEqual(wh_res.json()["status"], "ignored")

        campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        self.assertEqual(campaign.status, "pending_payment")
        self.assertEqual(campaign.payment_status, "unpaid")

    # 13. Duplicate webhook does not re-activate or extend duration
    @patch("app.services.xendit_service.create_invoice")
    def test_13_webhook_duplicate_does_not_re_activate_or_extend(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_dup_test",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-dup",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        wh_payload = {
            "id": "xinv_dup_test",
            "external_id": ext_id,
            "status": "PAID",
            "paid_amount": 15000,
            "currency": "IDR",
            "paid_at": "2026-10-10T12:00:00.000Z",
        }

        # First webhook
        res1 = self.client.post("/ads/webhook/xendit", json=wh_payload, headers={"x-callback-token": CALLBACK_TOKEN})
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res1.json()["status"], "success")

        campaign_after_first = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        expires_at_first = campaign_after_first.expires_at

        # Second duplicate webhook
        res2 = self.client.post("/ads/webhook/xendit", json=wh_payload, headers={"x-callback-token": CALLBACK_TOKEN})
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["status"], "already_processed")

        campaign_after_second = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        self.assertEqual(campaign_after_second.expires_at, expires_at_first)

    # 14. Expired or failed webhook does not overwrite paid transaction
    @patch("app.services.xendit_service.create_invoice")
    def test_14_webhook_expired_or_failed_does_not_overwrite_paid(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_paid_first",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-pf",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        # First webhook: PAID
        self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_paid_first", "external_id": ext_id, "status": "PAID", "paid_amount": 15000, "currency": "IDR"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )

        # Later event: EXPIRED (out of order webhook)
        self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_paid_first", "external_id": ext_id, "status": "EXPIRED", "amount": 15000, "currency": "IDR"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )

        # Transaction MUST still remain PAID
        tx = self.db.query(PaymentTransaction).filter(PaymentTransaction.external_id == ext_id).first()
        self.assertEqual(tx.status, "PAID")
        campaign = self.db.query(AdCampaign).filter(AdCampaign.id == campaign_id).first()
        self.assertEqual(campaign.status, "active")
        self.assertEqual(campaign.payment_status, "paid")

    # 15. Expired webhook marks pending transaction expired
    @patch("app.services.xendit_service.create_invoice")
    def test_15_webhook_expired_marks_pending_transaction_expired(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_to_expire",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-exp",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        exp_res = self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_to_expire", "external_id": ext_id, "status": "EXPIRED", "amount": 15000, "currency": "IDR"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )
        self.assertEqual(exp_res.status_code, 200)

        tx = self.db.query(PaymentTransaction).filter(PaymentTransaction.external_id == ext_id).first()
        self.assertEqual(tx.status, "EXPIRED")

    # 16. Webhook response and logs do not leak secrets
    def test_16_no_secret_leak_in_responses(self):
        wh_res = self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_leak_check", "external_id": "INV-UNKNOWN", "status": "PAID"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )
        body = wh_res.text
        self.assertNotIn(SANDBOX_KEY, body)
        self.assertNotIn(CALLBACK_TOKEN, body)

    # 17. Legacy campaigns without PaymentTransaction remain excluded from banners
    def test_17_legacy_campaigns_without_transaction_excluded_from_banners(self):
        now = datetime.now(timezone.utc)
        legacy_campaign = AdCampaign(
            store_id=self.store.id,
            owner_user_id=TEST_USER_ID,
            title="Iklan Lama Tanpa Payment Transaction",
            banner_url="https://example.com/legacy.jpg",
            duration_days=3,
            price_paid=15000,
            status="active",
            payment_status="paid",
            payment_ref="XND-LEGACY-FAKE-PROOF",
            payment_method="QRIS",
            start_at=now,
            expires_at=now + timedelta(days=3)
        )
        self.db.add(legacy_campaign)
        self.db.commit()

        # Query home banners
        res = self.client.get("/ads/home-banners?lat=-7.7550&lng=110.3800&radius_km=10")
        self.assertEqual(res.status_code, 200)
        titles = [b["title"] for b in res.json()]
        self.assertNotIn("Iklan Lama Tanpa Payment Transaction", titles)

    # 18. Verified paid campaign appears in home banners
    @patch("app.services.xendit_service.create_invoice")
    def test_18_verified_paid_campaign_appears_in_banners(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_verified_banner",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-vb",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        # Pay via webhook
        self.client.post(
            "/ads/webhook/xendit",
            json={"id": "xinv_verified_banner", "external_id": ext_id, "status": "PAID", "paid_amount": 15000, "currency": "IDR"},
            headers={"x-callback-token": CALLBACK_TOKEN}
        )

        # Query home banners
        res = self.client.get("/ads/home-banners?lat=-7.7550&lng=110.3800&radius_km=10")
        self.assertEqual(res.status_code, 200)
        titles = [b["title"] for b in res.json()]
        self.assertIn("Promo Akhir Pekan Superindo", titles)

    # 19. Payment status endpoint enforces merchant isolation
    @patch("app.services.xendit_service.create_invoice")
    def test_19_payment_status_endpoint_enforces_merchant_isolation(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_status_test",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-st",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        # Owner checks status -> 200
        status_res = self.client.get(f"/ads/campaigns/{campaign_id}/payment-status")
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.json()
        self.assertEqual(status_data["campaign_id"], campaign_id)
        self.assertEqual(status_data["transaction_status"], "PENDING")
        self.assertEqual(status_data["amount"], 15000)

        # Unauthorized user checks status -> 403
        app.dependency_overrides[get_current_user] = lambda: OTHER_USER_ID
        other_res = self.client.get(f"/ads/campaigns/{campaign_id}/payment-status")
        self.assertEqual(other_res.status_code, 403)

    # 20. Non-sandbox environment rejected safely
    def test_20_non_sandbox_environment_rejected(self):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]

        os.environ["XENDIT_ENVIRONMENT"] = "production"
        pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(pay_res.status_code, 503)
        self.assertIn("must be 'sandbox'", pay_res.json()["detail"])

        os.environ["XENDIT_ENVIRONMENT"] = "sandbox"
        os.environ["XENDIT_SECRET_KEY"] = "xnd_production_live_key_not_allowed"
        pay_res2 = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
        self.assertEqual(pay_res2.status_code, 503)
        self.assertIn("sandbox development key", pay_res2.json()["detail"])



    # 21. Webhook database failure returns 500 retryable
    @patch("app.services.xendit_service.create_invoice")
    def test_21_webhook_db_failure_returns_500_retryable(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_db_fail",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-fail",
            "amount": 15000,
        }
        self.client.post(f"/ads/campaigns/{campaign_id}/pay")

        # Mock db.commit failure during webhook processing
        with patch.object(self.db, "commit", side_effect=Exception("Database lock timeout")):
            app.dependency_overrides[get_db] = lambda: self.db
            wh_res = self.client.post(
                "/ads/webhook/xendit",
                json={"id": "xinv_db_fail", "external_id": ext_id, "status": "PAID", "paid_amount": 15000, "currency": "IDR"},
                headers={"x-callback-token": CALLBACK_TOKEN}
            )
            self.assertEqual(wh_res.status_code, 500)
            self.assertIn("Database error", wh_res.json()["detail"])

    # 22. Checkout database failure rolls back cleanly
    @patch("app.services.xendit_service.create_invoice")
    def test_22_checkout_db_failure_rolls_back(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]

        mock_create_inv.return_value = {
            "id": "xinv_db_fail_chk",
            "external_id": campaign_data["payment_ref"],
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-fail-chk",
            "amount": 15000,
        }

        # Mock db.commit failure during checkout transaction creation
        original_commit = self.db.commit
        call_count = [0]
        def fail_on_tx_commit():
            call_count[0] += 1
            # First commit was during create_pending_campaign, next is in pay
            raise Exception("Disk full")

        with patch.object(self.db, "commit", side_effect=fail_on_tx_commit):
            app.dependency_overrides[get_db] = lambda: self.db
            pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
            self.assertEqual(pay_res.status_code, 500)
            self.assertIn("Database error", pay_res.json()["detail"])



    # 23. Concurrent checkout race condition: IntegrityError triggers safe reconciliation
    @patch("app.services.xendit_service.create_invoice")
    def test_23_concurrent_checkout_reconciliation_on_integrity_error(self, mock_create_inv):
        campaign_data = self._create_pending_campaign()
        campaign_id = campaign_data["id"]
        ext_id = campaign_data["payment_ref"]

        mock_create_inv.return_value = {
            "id": "xinv_concurrent_race",
            "external_id": ext_id,
            "status": "PENDING",
            "invoice_url": "https://checkout.xendit.co/inv-race",
            "amount": 15000,
        }

        from sqlalchemy.exc import IntegrityError
        original_commit = self.db.commit
        has_raised = [False]

        def simulate_competing_insert_on_commit():
            if not has_raised[0]:
                has_raised[0] = True
                # Simulate competing worker in a separate session successfully committing first
                competing_session = TestingSessionLocal()
                try:
                    competing_tx = PaymentTransaction(
                        campaign_id=campaign_data["id"],
                        owner_user_id=TEST_USER_ID,
                        provider="xendit",
                        environment="sandbox",
                        external_id=ext_id,
                        xendit_invoice_id="xinv_concurrent_race",
                        amount=15000,
                        currency="IDR",
                        status="PENDING",
                        invoice_url="https://checkout.xendit.co/inv-race",
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                    competing_session.add(competing_tx)
                    competing_session.commit()
                finally:
                    competing_session.close()

                # Now the current worker's commit fails with IntegrityError (duplicate external_id)
                raise IntegrityError("UNIQUE constraint failed: payment_transactions.external_id", params=[], orig=Exception())
            else:
                original_commit()

        with patch.object(self.db, "commit", side_effect=simulate_competing_insert_on_commit):
            app.dependency_overrides[get_db] = lambda: self.db
            pay_res = self.client.post(f"/ads/campaigns/{campaign_id}/pay")
            # Must reconcile and return 200 with checkout URL, NOT 500 error!
            self.assertEqual(pay_res.status_code, 200)
            data = pay_res.json()
            self.assertEqual(data["external_id"], ext_id)
            self.assertEqual(data["invoice_url"], "https://checkout.xendit.co/inv-race")
            self.assertEqual(data["status"], "PENDING")

    # 24. Xendit API duplicate invoice response reconciles automatically
    @patch("app.services.xendit_service.get_invoice_by_external_id")
    def test_24_xendit_duplicate_invoice_reconciliation(self, mock_get_by_ext):
        import httpx
        from app.services import xendit_service

        mock_get_by_ext.return_value = {
            "id": "xinv_reconciled_999",
            "external_id": "INV-20261010-RECON",
            "status": "PENDING",
            "amount": 15000,
            "invoice_url": "https://checkout.xendit.co/inv-reconciled",
        }

        # Simulate client receiving duplicate error from Xendit
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {
            "error_code": "DUPLICATE_INVOICE_ERROR",
            "message": "An invoice with the same external id already exists"
        }
        mock_client.post.return_value = mock_resp

        result = xendit_service.create_invoice(
            external_id="INV-20261010-RECON",
            amount=15000,
            description="Test Promo",
            client=mock_client
        )
        self.assertEqual(result["id"], "xinv_reconciled_999")
        self.assertEqual(result["invoice_url"], "https://checkout.xendit.co/inv-reconciled")
        mock_get_by_ext.assert_called_once_with("INV-20261010-RECON", client=mock_client)


if __name__ == "__main__":
    unittest.main()
