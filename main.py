import os
from uuid import UUID
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel
from supabase import create_client, Client

# -------------------------
# ENV & SUPABASE
# -------------------------
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL veya SUPABASE_KEY .env içinde bulunamadı!")

# Debug: hangi key okunuyor? (istersen sonra sil)
print("KEY:", SUPABASE_KEY[:30])

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Vetcent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # şimdilik sadece local frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------
# MODELLER
# -------------------------
class SignupRequest(BaseModel):
    email: str
    password: str
    role: str  # clinic | supplier


class LoginRequest(BaseModel):
    email: str
    password: str


class SupplierPriceCreate(BaseModel):
    # ✅ supplier_id opsiyonel (istersen frontend gönderebilir; ileride token’dan alırız)
    supplier_id: Optional[UUID] = None
    product_id: UUID
    price: float
    stock: int = 0
    delivery_days: int = 1
    is_active: bool = True


class SupplierPriceUpdate(BaseModel):
    price: Optional[float] = None
    stock: Optional[int] = None
    delivery_days: Optional[int] = None
    is_active: Optional[bool] = None


class CartAddItem(BaseModel):
    clinic_user_id: UUID
    product_id: UUID
    supplier_id: UUID
    quantity: int = 1


class OrderCreateRequest(BaseModel):
    clinic_user_id: UUID


# -------------------------
# HELPERS
# -------------------------
def _recalc_and_update_order_total(order_id: str) -> float:
    items_res = (
        supabase
        .table("order_items")
        .select("quantity, unit_price")
        .eq("order_id", order_id)
        .execute()
    )
    items = items_res.data or []
    total = sum((i.get("quantity") or 0) * (i.get("unit_price") or 0) for i in items)

    supabase.table("orders").update({"total_amount": total}).eq("id", order_id).execute()
    return total


# -------------------------
# ROUTES
# -------------------------
@app.get("/")
def root():
    return {"message": "Vetcent backend çalışıyor!"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/products")
def get_products():
    try:
        res = (
            supabase
            .table("products")
            .select("""
                id,
                name,
                description,
                unit,
                brand,
                categories (
                    id,
                    name
                )
            """)
            .execute()
        )
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/products/search")
def search_products(
    q: Optional[str] = None,
    category_id: Optional[UUID] = None,
    brand: Optional[str] = None,
    unit: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    try:
        query = (
            supabase
            .table("products")
            .select("""
                id,
                name,
                description,
                unit,
                brand,
                categories (
                    id,
                    name
                )
            """)
        )

        if q and q.strip():
            pattern = f"%{q.strip()}%"
            query = query.or_(f"name.ilike.{pattern},brand.ilike.{pattern},description.ilike.{pattern}")

        if category_id:
            query = query.eq("category_id", str(category_id))

        if brand and brand.strip():
            query = query.ilike("brand", f"%{brand.strip()}%")

        if unit and unit.strip():
            query = query.ilike("unit", f"%{unit.strip()}%")

        query = query.order("name").range(offset, offset + limit - 1)
        res = query.execute()

        return {
            "q": q,
            "category_id": str(category_id) if category_id else None,
            "brand": brand,
            "unit": unit,
            "limit": limit,
            "offset": offset,
            "items": res.data
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/categories")
def get_categories():
    try:
        res = (
            supabase
            .table("categories")
            .select("id, name")
            .order("name")
            .execute()
        )
        return res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/brands")
def get_brands():
    try:
        res = (
            supabase
            .table("products")
            .select("brand")
            .neq("brand", None)
            .neq("brand", "")
            .execute()
        )
        brands = sorted({row["brand"] for row in res.data if row.get("brand")})
        return brands
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/units")
def get_units():
    try:
        res = (
            supabase
            .table("products")
            .select("unit")
            .neq("unit", None)
            .neq("unit", "")
            .execute()
        )
        units = sorted({row["unit"] for row in res.data if row.get("unit")})
        return units
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 21) SIGNUP (✅ FIX)
@app.post("/signup")
def signup(payload: SignupRequest):
    email = payload.email.strip().lower()
    role = payload.role.strip().lower()

    if role not in ["clinic", "supplier"]:
        raise HTTPException(status_code=400, detail="role sadece 'clinic' veya 'supplier' olabilir")

    try:
        # ✅ role bilgisini metadata olarak Supabase Auth'a gönderiyoruz
        # ✅ profiles + (supplier ise) suppliers tablosu TRIGGER ile otomatik oluşacak
        auth_res = supabase.auth.sign_up({
            "email": email,
            "password": payload.password,
            "options": {
                "data": {
                    "role": role,
                    "name": email
                }
            }
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auth sign_up hata: {str(e)}")

    user = getattr(auth_res, "user", None)
    if not user or not getattr(user, "id", None):
        raise HTTPException(
            status_code=400,
            detail="Kayıt oluşmadı (email doğrulama açık olabilir veya email kayıtlı olabilir)."
        )

    return {"message": "Kayıt başarılı", "user_id": user.id, "role": role}


# --- 22) LOGIN
@app.post("/login")
def login(payload: LoginRequest):
    email = payload.email.strip().lower()

    try:
        auth_res = supabase.auth.sign_in_with_password({"email": email, "password": payload.password})
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Giriş başarısız: {str(e)}")

    user = getattr(auth_res, "user", None)
    session = getattr(auth_res, "session", None)

    if not user or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Giriş başarısız: kullanıcı bulunamadı")

    user_id = user.id

    try:
        prof = (
            supabase
            .table("profiles")
            .select("role")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        role = prof.data.get("role") if prof.data else None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Role çekme hatası: {str(e)}")

    if not role:
        raise HTTPException(status_code=404, detail="Bu kullanıcı için profiles.role bulunamadı")

    return {
        "message": "Giriş başarılı",
        "user_id": user_id,
        "role": role,
        "access_token": session.access_token if session else None
    }


@app.get("/products/{product_id}/offers")
def get_product_offers(product_id: UUID):
    try:
        res = (
            supabase
            .table("supplier_prices")
            .select("id, price, stock, delivery_days, supplier_id")
            .eq("product_id", str(product_id))
            .eq("is_active", True)
            .gt("stock", 0)
            .order("price")
            .order("delivery_days")
            .execute()
        )
        return {"product_id": str(product_id), "offers": res.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/products/{product_id}/best-offer")
def get_product_best_offer(product_id: UUID):
    try:
        res = (
            supabase
            .table("supplier_prices")
            .select("""
                id,
                price,
                stock,
                delivery_days,
                supplier_id,
                suppliers (
                    id,
                    name
                )
            """)
            .eq("product_id", str(product_id))
            .eq("is_active", True)
            .gt("stock", 0)
            .order("price")
            .order("delivery_days")
            .limit(1)
            .execute()
        )
        best = res.data[0] if res.data else None
        return {"product_id": str(product_id), "best_offer": best}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- 28) SUPPLIER PRICE CRUD (✅ küçük iyileştirme)
@app.post("/supplier/prices")
def create_supplier_price(payload: SupplierPriceCreate, authorization: Optional[str] = Header(None)):
    if payload.price <= 0:
        raise HTTPException(status_code=400, detail="price > 0 olmalı")
    if payload.stock < 0:
        raise HTTPException(status_code=400, detail="stock negatif olamaz")
    if payload.delivery_days <= 0:
        raise HTTPException(status_code=400, detail="delivery_days > 0 olmalı")

    # ✅ supplier_id boş gelirse, şimdilik body zorunlu gibi davranalım
    # (Bir sonraki adımda token doğrulayıp supplier_id'yi token'dan alacağız)
    if payload.supplier_id is None:
        raise HTTPException(status_code=400, detail="supplier_id gerekli")

    try:
        res = (
            supabase
            .table("supplier_prices")
            .insert({
                "supplier_id": str(payload.supplier_id),
                "product_id": str(payload.product_id),
                "price": payload.price,
                "stock": payload.stock,
                "delivery_days": payload.delivery_days,
                "is_active": payload.is_active
            })
            .execute()
        )
        return {"message": "created", "item": res.data[0] if res.data else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/supplier/prices/{price_id}")
def update_supplier_price(price_id: UUID, payload: SupplierPriceUpdate):
    updates = {k: v for k, v in payload.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="Güncellenecek alan yok")

    try:
        res = (
            supabase
            .table("supplier_prices")
            .update(updates)
            .eq("id", str(price_id))
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
        return {"message": "updated", "item": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/supplier/prices/{price_id}/deactivate")
def deactivate_supplier_price(price_id: UUID):
    try:
        res = (
            supabase
            .table("supplier_prices")
            .update({"is_active": False})
            .eq("id", str(price_id))
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
        return {"message": "deactivated", "item": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------------
# 29) CART / DRAFT ORDER
# -------------------------
@app.post("/cart/add")
def add_to_cart(payload: CartAddItem):
    try:
        if payload.quantity <= 0:
            raise HTTPException(status_code=400, detail="quantity > 0 olmalı")

        order_res = (
            supabase
            .table("orders")
            .select("id")
            .eq("clinic_user_id", str(payload.clinic_user_id))
            .eq("status", "draft")
            .limit(1)
            .execute()
        )

        if order_res.data:
            order_id = order_res.data[0]["id"]
        else:
            new_order = (
                supabase
                .table("orders")
                .insert({
                    "clinic_user_id": str(payload.clinic_user_id),
                    "status": "draft",
                    "total_amount": 0
                })
                .execute()
            )
            order_id = new_order.data[0]["id"]

        sp = (
            supabase
            .table("supplier_prices")
            .select("price, stock, is_active")
            .eq("product_id", str(payload.product_id))
            .eq("supplier_id", str(payload.supplier_id))
            .eq("is_active", True)
            .single()
            .execute()
        )

        if not sp.data:
            raise HTTPException(status_code=404, detail="Bu ürün+tedarikçi için aktif supplier_prices bulunamadı")

        if (sp.data.get("stock") is not None) and sp.data["stock"] <= 0:
            raise HTTPException(status_code=400, detail="Stok yok (stock <= 0)")

        unit_price = sp.data["price"]

        item_res = (
            supabase
            .table("order_items")
            .select("id, quantity")
            .eq("order_id", order_id)
            .eq("product_id", str(payload.product_id))
            .eq("supplier_id", str(payload.supplier_id))
            .limit(1)
            .execute()
        )

        if item_res.data:
            item = item_res.data[0]
            updated = (
                supabase
                .table("order_items")
                .update({"quantity": item["quantity"] + payload.quantity})
                .eq("id", item["id"])
                .execute()
            )
            total = _recalc_and_update_order_total(order_id)
            return {
                "message": "cart_item_updated",
                "order_id": order_id,
                "total_amount": total,
                "item": updated.data[0] if updated.data else None
            }

        inserted = (
            supabase
            .table("order_items")
            .insert({
                "order_id": order_id,
                "product_id": str(payload.product_id),
                "supplier_id": str(payload.supplier_id),
                "quantity": payload.quantity,
                "unit_price": unit_price
            })
            .execute()
        )

        total = _recalc_and_update_order_total(order_id)
        return {
            "message": "added_to_cart",
            "order_id": order_id,
            "total_amount": total,
            "item": inserted.data[0] if inserted.data else None
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cart/add error: {str(e)}")


@app.get("/cart/{clinic_user_id}")
def get_cart(clinic_user_id: UUID):
    try:
        order_res = (
            supabase
            .table("orders")
            .select("id, total_amount")
            .eq("clinic_user_id", str(clinic_user_id))
            .eq("status", "draft")
            .limit(1)
            .execute()
        )

        if not order_res.data:
            return {"items": [], "total_amount": 0}

        order_id = order_res.data[0]["id"]
        total_amount = order_res.data[0].get("total_amount") or 0

        items_res = (
            supabase
            .table("order_items")
            .select("""
                id,
                quantity,
                unit_price,
                supplier_id,
                product_id,
                products (
                    id,
                    name
                ),
                suppliers (
                    id,
                    name
                )
            """)
            .eq("order_id", order_id)
            .execute()
        )

        return {
            "order_id": order_id,
            "items": items_res.data or [],
            "total_amount": total_amount
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"cart/get error: {str(e)}")


@app.post("/orders")
def create_order(payload: OrderCreateRequest):
    try:
        draft_res = (
            supabase
            .table("orders")
            .select("id, status, total_amount")
            .eq("clinic_user_id", str(payload.clinic_user_id))
            .eq("status", "draft")
            .limit(1)
            .execute()
        )

        if not draft_res.data:
            raise HTTPException(status_code=404, detail="Draft sepet bulunamadı. Önce /cart/add ile ürün ekle.")

        order_id = draft_res.data[0]["id"]

        items_res = (
            supabase
            .table("order_items")
            .select("id, product_id, supplier_id, quantity, unit_price")
            .eq("order_id", order_id)
            .execute()
        )
        items = items_res.data or []
        if len(items) == 0:
            raise HTTPException(status_code=400, detail="Sepet boş. Sipariş oluşturulamaz.")

        total_amount = _recalc_and_update_order_total(order_id)

        supabase.table("orders").update({"status": "submitted", "total_amount": total_amount}).eq("id", order_id).execute()

        return {
            "message": "order_created",
            "order_id": order_id,
            "status": "submitted",
            "total_amount": total_amount,
            "items_count": len(items)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"orders error: {str(e)}")


@app.post("/orders/submit")
def submit_order(payload: dict):
    clinic_user_id = payload.get("clinic_user_id")

    if not clinic_user_id:
        raise HTTPException(status_code=400, detail="clinic_user_id gerekli")

    order_res = (
        supabase
        .table("orders")
        .select("id")
        .eq("clinic_user_id", clinic_user_id)
        .eq("status", "draft")
        .limit(1)
        .execute()
    )

    if not order_res.data:
        raise HTTPException(status_code=404, detail="Draft order bulunamadı")

    order_id = order_res.data[0]["id"]

    supabase.table("orders").update({"status": "submitted"}).eq("id", order_id).execute()

    return {"message": "order_created", "order_id": order_id}


@app.get("/orders/{clinic_user_id}")
def get_orders(clinic_user_id: UUID):
    try:
        res = (
            supabase
            .table("orders")
            .select("id, status, total_amount, created_at")
            .eq("clinic_user_id", str(clinic_user_id))
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"clinic_user_id": str(clinic_user_id), "orders": res.data or []}


@app.get("/supplier/my-prices/{supplier_id}")
def supplier_my_prices(supplier_id: UUID):
    try:
        res = (
            supabase
            .table("supplier_prices")
            .select("""
                id,
                price,
                stock,
                delivery_days,
                is_active,
                created_at,
                product_id,
                supplier_id,
                products (
                    id,
                    name,
                    unit,
                    brand
                )
            """)
            .eq("supplier_id", str(supplier_id))
            .order("created_at", desc=True)
            .execute()
        )
        return {"supplier_id": str(supplier_id), "items": res.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
