import os
from collections import defaultdict
from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import io
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
import pytz 

app = Flask(__name__)
CORS(app)

db = None
TZ = pytz.timezone('America/Mexico_City') 

# 🟢 [AGREGADO] CATEGORÍAS FIJAS DE FALLBACK
DEFAULT_CATEGORIES = [
    'Transporte',
    'Comida',
    'Entretenimiento',
    'Servicios',
    'Renta',
    # 'Otros' se agrega automáticamente en el GET
]


# -------------------------------
# INICIALIZAR FIREBASE (SIN CAMBIOS)
# -------------------------------
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    cred = credentials.Certificate(
        os.path.join(BASE_DIR, "firebase_credentials.json")
    )
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Conexión a Firestore establecida con éxito.")

except Exception as e:
    print("❌ Error al conectar con Firestore:", e)


# -------------------------------
# ENDPOINT DE PRUEBA (SIN CAMBIOS)
# -------------------------------
@app.route("/")
def index():
    return jsonify({"status": "API funcionando ✅"})


# ============================================================================
# 🟢 NUEVOS ENDPOINTS: GESTIÓN DE CATEGORÍAS (CRUD)
# ============================================================================

def _get_user_categories(user_id):
    """Obtiene categorías personalizadas de Firestore y las combina con las por defecto."""
    if db is None:
        return DEFAULT_CATEGORIES
        
    # Obtener categorías personalizadas del usuario
    categories_ref = db.collection("categories").where("userId", "==", user_id).stream()
    
    # Usamos un conjunto para evitar duplicados
    all_categories = set(DEFAULT_CATEGORIES)
    
    for doc in categories_ref:
        data = doc.to_dict()
        category_name = data.get("name")
        if category_name:
            all_categories.add(category_name)
    
    # Convertir a lista y ordenar
    result = sorted(list(all_categories))
    
    # Asegurarse de que 'Otros' esté presente, normalmente al final.
    if 'Otros' not in result:
        result.append('Otros')
    else:
        # Mover 'Otros' al final si ya existía pero se había ordenado
        result.remove('Otros')
        result.append('Otros')
        
    return result

# L: READ - Obtener categorías
@app.route("/api/categories/<user_id>", methods=["GET"])
def get_categories(user_id):
    try:
        categories = _get_user_categories(user_id)
        return jsonify({"categories": categories}), 200
    except Exception as e:
        print(f"❌ Error en get_categories: {e}")
        return jsonify({"error": "Fallo interno al obtener categorías"}), 500

# C: CREATE - Crear una nueva categoría
@app.route("/api/categories/<user_id>", methods=["POST"])
def create_category(user_id):
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500

    try:
        data = request.get_json()
        new_category_name = data.get("name", "").strip().title()

        if not new_category_name:
            return jsonify({"error": "Nombre de categoría requerido"}), 400

        # Verificar si la categoría ya existe (personalizada o por defecto)
        existing_categories = _get_user_categories(user_id)
        if new_category_name in existing_categories:
            # 409 Conflict: Ya existe
            return jsonify({"error": "La categoría ya existe."}), 409

        # Guardar la nueva categoría
        category_doc = {
            "userId": user_id,
            "name": new_category_name,
            "createdAt": datetime.now()
        }
        db.collection("categories").add(category_doc)
        
        return jsonify({"status": "Categoría creada ✅", "name": new_category_name}), 201

    except Exception as e:
        print(f"❌ Error en create_category: {e}")
        return jsonify({"error": "Fallo interno al crear categoría"}), 500

# U: UPDATE - Modificar una categoría existente
@app.route("/api/categories/<user_id>", methods=["PUT"])
def update_category(user_id):
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500

    try:
        # Obtenemos el nombre viejo desde el query parameter
        old_name = request.args.get("old_name", "").strip().title()
        data = request.get_json()
        new_name = data.get("new_name", "").strip().title()

        if not old_name or not new_name:
            return jsonify({"error": "Los nombres de categoría viejo y nuevo son requeridos"}), 400
        
        # 1. Actualizar el documento en la colección 'categories'
        category_ref = db.collection("categories") \
                         .where("userId", "==", user_id) \
                         .where("name", "==", old_name).limit(1).stream()
        
        found = False
        for doc in category_ref:
            doc.reference.update({"name": new_name})
            found = True
            break
        
        if not found and old_name not in DEFAULT_CATEGORIES:
            return jsonify({"error": f"Categoría '{old_name}' no encontrada o no es personalizable."}), 404

        # 2. Actualizar todas las transacciones antiguas
        # NOTA: En Firestore, las consultas de actualización masivas deben ser por lotes o transacciones
        # Aquí usamos un lote (Batch) para la eficiencia
        transactions_to_update = db.collection("transactions") \
                                   .where("userId", "==", user_id) \
                                   .where("category", "==", old_name) \
                                   .stream()
                                   
        batch = db.batch()
        count = 0
        for transaction_doc in transactions_to_update:
            batch.update(transaction_doc.reference, {"category": new_name})
            count += 1

        batch.commit()
        
        return jsonify({"status": f"Categoría actualizada de '{old_name}' a '{new_name}' y {count} transacciones actualizadas."}), 200

    except Exception as e:
        print(f"❌ Error en update_category: {e}")
        return jsonify({"error": "Fallo interno al modificar categoría"}), 500

# D: DELETE - Eliminar una categoría
@app.route("/api/categories/<user_id>", methods=["DELETE"])
def delete_category(user_id):
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500

    try:
        # Obtenemos el nombre a eliminar desde el query parameter
        category_to_delete = request.args.get("name", "").strip().title()

        if not category_to_delete:
            return jsonify({"error": "Nombre de categoría a eliminar requerido"}), 400
            
        if category_to_delete in DEFAULT_CATEGORIES:
             return jsonify({"error": f"La categoría '{category_to_delete}' es estándar y no puede ser eliminada."}), 403


        # 1. Eliminar el documento de la colección 'categories'
        category_ref = db.collection("categories") \
                         .where("userId", "==", user_id) \
                         .where("name", "==", category_to_delete).limit(1).stream()
        
        deleted_count = 0
        for doc in category_ref:
            doc.reference.delete()
            deleted_count += 1
        
        if deleted_count == 0:
            return jsonify({"error": f"Categoría '{category_to_delete}' no encontrada en las categorías personalizadas."}), 404

        # 2. Actualizar transacciones: Asignar transacciones eliminadas a 'Otros'
        transactions_to_update = db.collection("transactions") \
                                   .where("userId", "==", user_id) \
                                   .where("category", "==", category_to_delete) \
                                   .stream()
                                   
        batch = db.batch()
        count = 0
        for transaction_doc in transactions_to_update:
            # Reasignar a 'Otros' (o 'Sin Categoría' si lo prefieres)
            batch.update(transaction_doc.reference, {"category": "Otros"}) 
            count += 1

        batch.commit()
        
        return jsonify({"status": f"Categoría '{category_to_delete}' eliminada y {count} transacciones reasignadas a 'Otros'."}), 200

    except Exception as e:
        print(f"❌ Error en delete_category: {e}")
        return jsonify({"error": "Fallo interno al eliminar categoría"}), 500


# ============================================================================
# 🤖 FUNCIÓN DE LÓGICA: CATEGORÍA PROBLEMÁTICA (SIN CAMBIOS)
# ============================================================================
def get_most_problematic_category(user_id):
    # ... (código existente, no requiere cambios)
    if db is None:
        return {"category": None, "current_spend": 0.0, "goal_amount": 0.0, "exceeds_goal": False}

    try:
        # 1. Obtener la meta (Goal) desde la colección 'settings'
        goal_ref = db.collection("settings").document(user_id).get()
        goal_amount_str = goal_ref.to_dict().get("monthlyGoal", "500.0") if goal_ref.exists else "500.0"
        goal_amount = float(goal_amount_str)

        # 2. Definir el rango del mes actual
        now = datetime.now(TZ)
        start_of_month = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=TZ)
        
        # 3. Consultar gastos del mes actual (filtrando por fecha y tipo)
        transactions_ref = db.collection("transactions") \
            .where("userId", "==", user_id) \
            .where("type", "==", "expense") \
            .where("date", ">=", start_of_month) \
            .stream()

        category_totals = defaultdict(float)

        for doc in transactions_ref:
            data = doc.to_dict()
            category = data.get("category", "Otros").strip().title()
            amount = float(data.get("amount", 0))
            category_totals[category] += amount
        
        # 4. Encontrar la categoría con el gasto más alto este mes
        most_problematic_category = "Otros"
        max_spend = 0.0
        
        if category_totals:
            most_problematic_category = max(category_totals, key=category_totals.get)
            max_spend = category_totals[most_problematic_category]
            
        # 5. Comparar el gasto MÁS ALTO con la meta total
        exceeds_goal = max_spend > goal_amount
            
        return {
            "category": most_problematic_category,
            "current_spend": max_spend,
            "goal_amount": goal_amount,
            "exceeds_goal": exceeds_goal
        }
    except Exception as e:
        print(f"Error en get_most_problematic_category: {e}")
        return {"category": "Error", "current_spend": 0.0, "goal_amount": 0.0, "exceeds_goal": False}

# ============================================================================
# 🚨 ENDPOINT: CATEGORÍA PROBLEMÁTICA (SIN CAMBIOS)
# ============================================================================
@app.route("/api/reports/problematic_category/<user_id>", methods=["GET"])
def problematic_category(user_id):
    try:
        data = get_most_problematic_category(user_id)
        return jsonify(data)
    except Exception as e:
        print(f"Error en problematic_category: {e}")
        return jsonify({"error": "Fallo interno al procesar categoría problemática"}), 500


# ============================================================================
# 🚀 FUNCIÓN REAL: GENERA PDF CON DATOS DE FIREBASE (SIN CAMBIOS)
# ============================================================================
def generar_pdf_real(user_id):
    # ... (código del PDF omitido por brevedad, no requiere cambios)
    if db is None:
        print("Error: DB no inicializada para PDF.")
        return None
        
    # 1. CONSULTAR FIREBASE
    # ---------------------------------------------------------
    try:
        transactions_ref = db.collection("transactions").where("userId", "==", user_id).stream()
        
        lista_movimientos = []
        total_ingresos = 0.0
        total_gastos = 0.0

        for doc in transactions_ref:
            data = doc.to_dict()
            if not data: continue

            monto = float(data.get("amount", 0))
            tipo = data.get("type", "expense")
            categoria = data.get("category", "Varios").strip().title()
            fecha_obj = data.get("date")

            # Convertir fecha a string legible
            fecha_str = "S/F"
            if fecha_obj:
                try:
                    if not isinstance(fecha_obj, datetime):
                        fecha_obj = fecha_obj.to_pydatetime()
                    fecha_str = fecha_obj.strftime("%Y-%m-%d")
                except:
                    fecha_str = "Fecha Inválida"

            # Calcular totales
            if tipo == "income":
                total_ingresos += monto
            elif tipo == "expense":
                total_gastos += monto
            
            lista_movimientos.append({
                "fecha": fecha_str,
                "cat": categoria,
                "monto": monto,
                "tipo": tipo
            })

        balance_total = total_ingresos - total_gastos

    except Exception as e:
        print(f"❌ Error leyendo base de datos para PDF: {e}")
        return None

    # 2. DIBUJAR EL PDF (Usando reportlab)
    # ... (código de dibujo del PDF)
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    
    # Encabezado
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "REPORTE FINANCIERO PERSONAL")
    
    c.setFont("Helvetica", 10)
    c.drawString(50, height - 70, f"Usuario: {user_id}")
    c.drawString(50, height - 85, f"Fecha de emisión: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Resumen (Cuadro de Totales)
    c.line(50, height - 100, 550, height - 100)
    
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, height - 130, f"Ingresos Totales: ${total_ingresos:,.2f}")
    c.drawString(50, height - 150, f"Gastos Totales:  ${total_gastos:,.2f}")
    
    c.setFont("Helvetica-Bold", 14)
    if balance_total >= 0:
        c.setFillColorRGB(0, 0.5, 0) # Verde
    else:
        c.setFillColorRGB(0.8, 0, 0) # Rojo
        
    c.drawString(300, height - 140, f"BALANCE: ${balance_total:,.2f}")
    c.setFillColorRGB(0, 0, 0) # Volver a negro

    c.line(50, height - 170, 550, height - 170)

    # Listado de Movimientos (Tabla simple)
    y = height - 200
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "FECHA")
    c.drawString(150, y, "CATEGORÍA")
    c.drawString(350, y, "TIPO")
    c.drawString(450, y, "MONTO")
    
    y -= 20
    c.setFont("Helvetica", 9)

    for mov in lista_movimientos:
        # Paginación simple
        if y < 50:
            c.showPage()
            y = height - 50
            c.setFont("Helvetica-Bold", 10)
            c.drawString(50, y, "FECHA")
            c.drawString(150, y, "CATEGORÍA")
            c.drawString(350, y, "TIPO")
            c.drawString(450, y, "MONTO")
            c.setFont("Helvetica", 9)
            y -= 20 # Bajar renglón después del nuevo encabezado

        c.drawString(50, y, mov["fecha"])
        c.drawString(150, y, mov["cat"])
        
        tipo_lbl = "Ingreso" if mov["tipo"] == "income" else "Gasto"
        c.drawString(350, y, tipo_lbl)
        
        monto_str = f"${mov['monto']:,.2f}"
        c.drawString(450, y, monto_str)
        
        y -= 15 # Bajar renglón

    c.save()
    
    buffer.seek(0)
    return buffer.read()


# ============================================================================
# 🚨 ENDPOINT: DESCARGA DE REPORTE PDF (SIN CAMBIOS)
# ============================================================================
@app.route("/api/reports/download", methods=['GET'])
def download_report():
    # ... (código existente, no requiere cambios)
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500
        
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "user_id requerido"}), 400

    try:
        pdf_bytes = generar_pdf_real(user_id) 
        
        if pdf_bytes is None:
             return jsonify({"error": "Error interno al generar el PDF o datos no encontrados"}), 500
             
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'reporte_gastos_{user_id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
        )
    except Exception as e:
        print("❌ Error grave en download_report:", e)
        return jsonify({
            "error": "Fallo interno al procesar el PDF",
            "details": str(e)
        }), 500


# ============================================================================
# 🚀 REPORTE MENSUAL (con trend para gráficas) (SIN CAMBIOS)
# ============================================================================
@app.route("/api/reports/monthly", methods=["GET"])
def monthly_report():
    # ... (código existente, no requiere cambios)
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500

    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id requerido"}), 400

    try:
        transactions = (
            db.collection("transactions")
            .where("userId", "==", user_id)
            .stream()
        )

        monthly_data = defaultdict(lambda: defaultdict(float))
        types_dict = {}

        for doc in transactions:
            data = doc.to_dict()
            if not data:
                continue

            if data.get("type") not in ["expense", "income"]:
                continue

            category = data.get("category", "Otros")
            # Normaliza categorías (Comida = comida)
            category = category.strip().title()

            amount = float(data.get("amount", 0))
            date_obj = data.get("date")

            if not isinstance(date_obj, datetime):
                 date_obj = date_obj.to_pydatetime() 

            month_key = date_obj.strftime("%Y-%m") 

            monthly_data[category][month_key] += amount
            types_dict[category] = data.get("type")

        # Construye respuesta ordenada
        response = []

        for category, months in monthly_data.items():

            sorted_months = sorted(months.keys())
            trend = [months[m] for m in sorted_months]

            if len(trend) == 1:
                trend = [0, trend[0]]

            response.append({
                "category_name": category,
                "total_amount": sum(trend),
                "monthly_trend": trend,
                "type": types_dict.get(category, "expense")
            })

        return jsonify(response)

    except Exception as e:
        print("❌ Error grave en monthly_report:", e)
        return jsonify({
            "error": "Fallo interno del servidor",
            "details": str(e)
        }), 500


# -------------------------------
# AGREGAR TRANSACCIÓN (SIN CAMBIOS)
# -------------------------------
@app.route("/api/transactions/<user_id>", methods=["POST"])
def add_transaction(user_id):
    # ... (código existente, no requiere cambios)
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500

    try:
        data = request.get_json()
        required_fields = ["type", "amount", "category", "description", "date"]
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Falta campo {field}"}), 400

        amount = float(data["amount"])
        date_obj = datetime.strptime(data["date"], "%Y-%m-%d") 

        transaction_doc = {
            "userId": user_id,
            "type": data["type"],
            "amount": amount,
            "category": data["category"].strip().title(), # 🎯 [IMPORTANTE] Normalizar la categoría aquí también
            "description": data["description"],
            "date": date_obj 
        }

        db.collection("transactions").add(transaction_doc)
        return jsonify({"status": "Transacción guardada ✅"}), 201

    except Exception as e:
        print("❌ Error en add_transaction:", e)
        return jsonify({
            "error": "Fallo interno del servidor",
            "details": str(e)
        }), 500
    

    # -------------------------------
# OBTENER ÚLTIMAS TRANSACCIONES
# -------------------------------
@app.route("/api/transactions/<user_id>/latest", methods=["GET"])
def get_latest_transactions(user_id):
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500

    try:
        limit = int(request.args.get("limit", 5))

        transactions_ref = (
            db.collection("transactions")
            .where("userId", "==", user_id)
            .order_by("date", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )

        transactions = []
        for doc in transactions_ref:
            data = doc.to_dict()
            data["id"] = doc.id

            if isinstance(data.get("date"), datetime):
                data["date"] = data["date"].strftime("%Y-%m-%d")

            transactions.append(data)

        return jsonify({"transactions": transactions}), 200

    except Exception as e:
        print("❌ Error en get_latest_transactions:", e)
        return jsonify({
            "error": "Error al obtener transacciones",
            "details": str(e)
        }), 500



# -------------------------------
# ENDPOINT: SALDO ACTUAL (SIN CAMBIOS)
# -------------------------------
@app.route("/api/balance/<user_id>", methods=["GET"])
def get_balance(user_id):
    # ... (código existente, no requiere cambios)
    if db is None:
        return jsonify({"error": "Firestore no inicializado"}), 500

    try:
        transactions = (
            db.collection("transactions")
            .where("userId", "==", user_id)
            .stream()
        )

        balance = 0.0
        for doc in transactions:
            data = doc.to_dict()
            if not data:
                continue

            amount = float(data.get("amount", 0))

            if data.get("type") == "income":
                balance += amount
            elif data.get("type") == "expense":
                balance -= amount

        return jsonify({"current_balance": balance}), 200

    except Exception as e:
        print("❌ Error en get_balance:", e)
        return jsonify({
            "error": "Fallo interno del servidor",
            "details": str(e)
        }), 500


# -------------------------------
# MAIN
# -------------------------------
if __name__ == "__main__":
    print("🚀 API corriendo en http://0.0.0.0:5000")
    print("\n📌 Rutas disponibles en la API:")
    print("/ -> GET")
    print("/api/reports/monthly -> GET (Reporte de Tendencia)")
    print("/api/reports/download -> GET (Descarga de PDF REAL)")
    print("/api/reports/problematic_category/<user_id> -> GET (Categoría Problemática) 🚨")
    print("/api/transactions/<user_id> -> POST (Agregar Transacción)")
    print("/api/categories/<user_id> -> GET, POST, PUT, DELETE (Gestión de Categorías) 🟢 NUEVO")
    print("/api/balance/<user_id> -> GET (Obtener Saldo)")
    print("----------------------------------------------------------------\n")

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True
    )


