from pydantic import BaseModel
from typing import Optional
import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import logging
import json
from openai import OpenAI
from datetime import datetime

class TravelRequest(BaseModel):
    passengers: Optional[int] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    departure_date: Optional[str] = None
    return_date: Optional[str] = None
    budget_per_person: Optional[float] = None
    
    def is_complete(self) -> bool:
        return all([
            self.passengers is not None,
            self.origin is not None,
            self.destination is not None,
            self.departure_date is not None,
            self.return_date is not None,
            self.budget_per_person is not None
        ])
    
    def get_missing_fields(self) -> list[str]:
        missing = []
        if self.passengers is None:
            missing.append("número de pasajeros")
        if self.origin is None:
            missing.append("ciudad de origen")
        if self.destination is None:
            missing.append("ciudad de destino")
        if self.departure_date is None:
            missing.append("fecha de salida")
        if self.return_date is None:
            missing.append("fecha de regreso")
        if self.budget_per_person is None:
            missing.append("presupuesto por persona")
        return missing


# Estructura para guardar temporalmente el estado del usuario
user_data_store = {}

# Estructura para controlar usuarios autorizados y límite de mensajes
user_message_count = {}
MAX_MESSAGES_PER_USER = 15

def is_user_authorized(user_id: int) -> bool:
    """Verifica si el usuario está autorizado para usar el bot"""
    authorized_user_ids = os.getenv("USER_ID_AUTORIZADO")
    if not authorized_user_ids:
        return False
    
    try:
        # Separar los IDs por comas y limpiar espacios
        authorized_ids = [int(id_str.strip()) for id_str in authorized_user_ids.split(',')]
        return user_id in authorized_ids
    except ValueError:
        # Si hay error al convertir algún ID, intentar con formato original (un solo ID)
        try:
            return user_id == int(authorized_user_ids.strip())
        except ValueError:
            return False

def can_user_send_message(user_id: int) -> tuple[bool, int]:
    """
    Verifica si el usuario puede enviar más mensajes
    Retorna (puede_enviar, mensajes_restantes)
    """
    if user_id not in user_message_count:
        user_message_count[user_id] = 0
    
    messages_sent = user_message_count[user_id]
    remaining = MAX_MESSAGES_PER_USER - messages_sent
    
    return messages_sent < MAX_MESSAGES_PER_USER, remaining

def increment_message_count(user_id: int):
    """Incrementa el contador de mensajes del usuario"""
    if user_id not in user_message_count:
        user_message_count[user_id] = 0
    user_message_count[user_id] += 1

def get_authorized_users_list() -> list[int]:
    """Obtiene la lista de usuarios autorizados"""
    authorized_user_ids = os.getenv("USER_ID_AUTORIZADO")
    if not authorized_user_ids:
        return []
    
    try:
        # Separar los IDs por comas y limpiar espacios
        authorized_ids = [int(id_str.strip()) for id_str in authorized_user_ids.split(',')]
        return authorized_ids
    except ValueError:
        # Si hay error al convertir algún ID, intentar con formato original (un solo ID)
        try:
            return [int(authorized_user_ids.strip())]
        except ValueError:
            return []

def reset_user_count(user_id: int):
    """Resetea el contador de mensajes del usuario (solo para uso administrativo)"""
    if user_id in user_message_count:
        user_message_count[user_id] = 0

# Configurar cliente de OpenAI
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def is_travel_related(user_message: str) -> tuple[bool, str]:
    """
    Verifica si el mensaje del usuario está relacionado con viajes
    """
    
    validation_prompt = """
    Tu trabajo es determinar si un mensaje está relacionado con viajes o planificación de viajes.
    
    Temas PERMITIDOS (relacionados con viajes):
    - Destinos, ciudades, países
    - Fechas de viaje, duración
    - Número de pasajeros, acompañantes
    - Presupuestos, costos de viaje
    - Transporte (avión, tren, coche)
    - Alojamiento (hoteles, apartamentos)
    - Actividades turísticas
    - Documentación de viaje (pasaporte, visa)
    - Correcciones o cambios en información de viaje
    
    Temas NO PERMITIDOS:
    - Política, religión, ideologías
    - Contenido sexual o inapropiado
    - Violencia o contenido dañino
    - Temas médicos complejos
    - Finanzas no relacionadas con viajes
    - Tecnología no relacionada con viajes
    - Conversaciones generales no relacionadas
    
    Responde SOLO en formato JSON:
    {
        "is_travel_related": true_o_false,
        "reason": "breve_explicación_si_no_está_relacionado"
    }
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": validation_prompt},
                {"role": "user", "content": f"Mensaje del usuario: '{user_message}'"}
            ],
            temperature=0.3,
            max_tokens=150
        )
        
        content = response.choices[0].message.content
        if not content:
            return True, ""  # En caso de error, permitir el mensaje
            
        validation_result = json.loads(content)
        return validation_result.get("is_travel_related", True), validation_result.get("reason", "")
        
    except Exception as e:
        logging.error(f"Error en validación de tema: {e}")
        return True, ""  # En caso de error, permitir el mensaje

async def extract_travel_info(user_message: str, current_request: TravelRequest) -> tuple[TravelRequest, str]:
    """
    Usa OpenAI para extraer información de viaje del mensaje del usuario
    y generar una respuesta apropiada.
    """
    today = datetime.today().strftime('%Y-%m-%d')

    # Crear el prompt para OpenAI con más contexto y validaciones
    system_prompt = """
    Eres un asistente de viajes profesional que ayuda a recopilar información para planificar viajes.
    
    REGLAS IMPORTANTES:
    1. SOLO hablas de temas relacionados con viajes y turismo
    2. Si el usuario pregunta sobre otros temas, redirígelo amablemente a hablar de viajes
    3. Valida que las fechas sean lógicas (salida antes que regreso, no en el pasado)
    4. Valida que los presupuestos sean realistas (mayor que 0, menor que 50000€)
    5. Valida que el número de pasajeros sea realista (1-20 personas)
    6. Normaliza nombres de ciudades y países a su forma estándar
    
    Tu trabajo es:
    1. Extraer información de viaje del mensaje del usuario
    2. Actualizar la información que ya tienes
    3. Generar una respuesta natural para continuar la conversación
    4. Si falta información, preguntar de manera natural por los datos faltantes
    5. Validar que la información sea coherente y realista
    
    La información que necesitas recopilar:
    - passengers: número de pasajeros (entero entre 1-20)
    - origin: ciudad de origen (string, formato: "Ciudad, País")
    - destination: ciudad de destino (string, formato: "Ciudad, País")
    - departure_date: fecha de salida (formato YYYY-MM-DD, no en el pasado)
    - return_date: fecha de regreso (formato YYYY-MM-DD, después de departure_date)
    - budget_per_person: presupuesto por persona en euros (número decimal entre 50-50000)
    
    Responde SIEMPRE en formato JSON con esta estructura:
    {
        "extracted_info": {
            "passengers": null_o_numero,
            "origin": null_o_string,
            "destination": null_o_string,
            "departure_date": null_o_string_YYYY-MM-DD,
            "return_date": null_o_string_YYYY-MM-DD,
            "budget_per_person": null_o_numero
        },
        "response": "respuesta_natural_al_usuario",
        "validation_issues": ["lista_de_problemas_si_los_hay"]
    }
    
    Mantén las respuestas amigables y profesionales. Si toda la información está completa y validada, 
    confirma los detalles y indica que procesarás la solicitud.
    """
    
    user_prompt = f"""
    Información actual del viaje:
    - Pasajeros: {current_request.passengers}
    - Origen: {current_request.origin}
    - Destino: {current_request.destination}
    - Fecha salida: {current_request.departure_date}
    - Fecha regreso: {current_request.return_date}
    - Presupuesto por persona: {current_request.budget_per_person}€
    
    Nuevo mensaje del usuario: "{user_message}"
    
    Fecha actual: {today}
    
    Extrae cualquier información nueva del mensaje, valida que sea coherente y genera una respuesta apropiada.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=600
        )
        
        # Parsear la respuesta JSON
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Respuesta vacía de OpenAI")
            
        ai_response = json.loads(content)
        
        # Actualizar el TravelRequest con la información extraída
        extracted = ai_response["extracted_info"]
        
        # Validaciones adicionales del lado del código
        validation_issues = ai_response.get("validation_issues", [])
        
        updated_request = TravelRequest(
            passengers=extracted.get("passengers") or current_request.passengers,
            origin=extracted.get("origin") or current_request.origin,
            destination=extracted.get("destination") or current_request.destination,
            departure_date=extracted.get("departure_date") or current_request.departure_date,
            return_date=extracted.get("return_date") or current_request.return_date,
            budget_per_person=extracted.get("budget_per_person") or current_request.budget_per_person
        )
        
        response_text = ai_response["response"]
        
        # Añadir advertencias si hay problemas de validación
        if validation_issues:
            response_text += f"\n\n⚠️ **Nota:** {'; '.join(validation_issues)}"
        
        return updated_request, response_text
        
    except Exception as e:
        logging.error(f"Error con OpenAI: {e}")
        missing_fields = current_request.get_missing_fields()
        if missing_fields:
            return current_request, f"Necesito que me proporciones: {', '.join(missing_fields)}"
        else:
            return current_request, "¡Perfecto! Tengo toda la información necesaria."

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Verificar si el usuario está autorizado
    if not is_user_authorized(user_id):
        await update.message.reply_text(
            "❌ **Acceso denegado**\n\n"
            "Este bot está restringido a usuarios autorizados.\n"
            f"Tu ID de usuario: `{user_id}`\n\n"
            "Contacta al administrador para obtener acceso."
        )
        return
    
    # Verificar límite de mensajes
    can_send, remaining = can_user_send_message(user_id)
    if not can_send:
        await update.message.reply_text(
            "⚠️ **Límite de mensajes alcanzado**\n\n"
            f"Has alcanzado el límite de {MAX_MESSAGES_PER_USER} mensajes.\n"
            "Contacta al administrador para resetear tu contador."
        )
        return
    
    # Incrementar contador de mensajes
    increment_message_count(user_id)
    
    user_data_store[user_id] = TravelRequest()
    
    welcome_message = f"""
¡Hola! 👋 Soy tu asistente de viajes inteligente.

Puedes contarme sobre tu viaje de forma natural. Por ejemplo:
• "Quiero ir a París desde Madrid para 2 personas"
• "Necesito un viaje del 15 al 22 de agosto con un presupuesto de 800€ por persona"
• O simplemente dime paso a paso la información

¿Qué viaje estás planeando?

📊 **Mensajes restantes:** {remaining - 1}
    """
    
    await update.message.reply_text(welcome_message)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.message or not update.message.text:
        return
        
    user_id = update.effective_user.id
    
    # Verificar si el usuario está autorizado
    if not is_user_authorized(user_id):
        await update.message.reply_text(
            "❌ **Acceso denegado**\n\n"
            "Este bot está restringido a usuarios autorizados.\n"
            f"Tu ID de usuario: `{user_id}`\n\n"
            "Contacta al administrador para obtener acceso."
        )
        return
    
    # Verificar límite de mensajes
    can_send, remaining = can_user_send_message(user_id)
    if not can_send:
        await update.message.reply_text(
            "⚠️ **Límite de mensajes alcanzado**\n\n"
            f"Has alcanzado el límite de {MAX_MESSAGES_PER_USER} mensajes.\n"
            "Contacta al administrador para resetear tu contador."
        )
        return
    
    # Incrementar contador de mensajes
    increment_message_count(user_id)
    
    if user_id not in user_data_store:
        await update.message.reply_text("Por favor, usa /start para comenzar.")
        return

    # Obtener información actual del usuario
    current_request = user_data_store[user_id]
    user_message = update.message.text.strip()
    
    # Verificar si el mensaje está relacionado con viajes
    is_travel, reason = await is_travel_related(user_message)
    if not is_travel:
        not_travel_response = f"""
🚫 **Tema no relacionado con viajes**

{reason}

Soy un asistente especializado en planificación de viajes. Puedo ayudarte con:
• Destinos y ciudades
• Fechas y duración del viaje  
• Número de pasajeros
• Presupuestos de viaje
• Recomendaciones turísticas

¿En qué puedo ayudarte con tu próximo viaje? ✈️

📊 **Mensajes restantes:** {remaining - 1}
        """
        await update.message.reply_text(not_travel_response)
        return
    
    # Usar OpenAI para procesar el mensaje
    updated_request, ai_response = await extract_travel_info(user_message, current_request)
    
    # Actualizar la información del usuario
    user_data_store[user_id] = updated_request
    
    # Añadir información sobre mensajes restantes
    remaining_after = remaining - 1
    response_with_count = f"{ai_response}\n\n📊 **Mensajes restantes:** {remaining_after}"
    
    # Enviar respuesta al usuario
    await update.message.reply_text(response_with_count)
    
    # Si tenemos toda la información, mostrar resumen final y recomendaciones
    if updated_request.is_complete():
        summary = f"""
🎯 **Resumen de tu viaje:**

👥 **Pasajeros:** {updated_request.passengers}
🏙️ **Origen:** {updated_request.origin}
🌍 **Destino:** {updated_request.destination}
📅 **Salida:** {updated_request.departure_date}
📅 **Regreso:** {updated_request.return_date}
💰 **Presupuesto por persona:** {updated_request.budget_per_person}€

¡Toda la información está completa! 🎉
        """
        await update.message.reply_text(summary)
        
        # Enviar mensaje de "generando recomendaciones"
        generating_msg = await update.message.reply_text("🔄 Generando recomendaciones personalizadas para tu viaje...")
        
        # Generar recomendaciones
        recommendations = await generate_travel_recommendations(updated_request)
        
        # Eliminar mensaje de "generando" y enviar recomendaciones
        await generating_msg.delete()
        
        final_message = f"""
🌟 **Recomendaciones para tu viaje:**

{recommendations}

¡Disfruta tu viaje! Si necesitas ayuda con otro viaje, usa /start 😊
        """
        await update.message.reply_text(final_message)
        
        # Limpiar los datos del usuario
        del user_data_store[user_id]
        
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el estado actual de la información del viaje"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Verificar si el usuario está autorizado
    if not is_user_authorized(user_id):
        await update.message.reply_text(
            "❌ **Acceso denegado**\n\n"
            "Este bot está restringido a usuarios autorizados."
        )
        return
    
    if user_id not in user_data_store:
        # Mostrar información de mensajes aunque no tenga viaje activo
        can_send, remaining = can_user_send_message(user_id)
        messages_used = MAX_MESSAGES_PER_USER - remaining
        
        status_message = f"""
📊 **Estado del usuario:**

❌ No tienes ningún viaje en proceso. Usa /start para comenzar.

📈 **Uso de mensajes:**
• Mensajes usados: {messages_used}/{MAX_MESSAGES_PER_USER}
• Mensajes restantes: {remaining}
        """
        await update.message.reply_text(status_message)
        return
    
    current_request = user_data_store[user_id]
    missing_fields = current_request.get_missing_fields()
    
    # Información de mensajes
    can_send, remaining = can_user_send_message(user_id)
    messages_used = MAX_MESSAGES_PER_USER - remaining
    
    status_message = "📊 **Estado actual de tu viaje:**\n\n"
    
    status_message += f"👥 **Pasajeros:** {current_request.passengers or '❌ Falta'}\n"
    status_message += f"🏙️ **Origen:** {current_request.origin or '❌ Falta'}\n"
    status_message += f"🌍 **Destino:** {current_request.destination or '❌ Falta'}\n"
    status_message += f"📅 **Salida:** {current_request.departure_date or '❌ Falta'}\n"
    status_message += f"📅 **Regreso:** {current_request.return_date or '❌ Falta'}\n"
    status_message += f"💰 **Presupuesto:** {current_request.budget_per_person or '❌ Falta'}€\n\n"
    
    if missing_fields:
        status_message += f"📝 **Falta por completar:** {', '.join(missing_fields)}\n\n"
    else:
        status_message += "✅ **¡Información completa!**\n\n"
    
    status_message += f"📈 **Uso de mensajes:**\n"
    status_message += f"• Mensajes usados: {messages_used}/{MAX_MESSAGES_PER_USER}\n"
    status_message += f"• Mensajes restantes: {remaining}"
    
    await update.message.reply_text(status_message)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancela el viaje actual"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Verificar si el usuario está autorizado
    if not is_user_authorized(user_id):
        await update.message.reply_text(
            "❌ **Acceso denegado**\n\n"
            "Este bot está restringido a usuarios autorizados."
        )
        return
    
    if user_id in user_data_store:
        del user_data_store[user_id]
        await update.message.reply_text("❌ Viaje cancelado. Usa /start para comenzar uno nuevo.")
    else:
        await update.message.reply_text("No tienes ningún viaje en proceso.")

async def reset_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando administrativo para resetear el contador de mensajes"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Solo el usuario autorizado puede usar este comando
    if not is_user_authorized(user_id):
        await update.message.reply_text("❌ Comando no autorizado.")
        return
    
    # Resetear el contador del usuario autorizado
    reset_user_count(user_id)
    await update.message.reply_text(
        f"✅ **Contador reseteado**\n\n"
        f"Tu contador de mensajes ha sido reseteado.\n"
        f"Ahora tienes {MAX_MESSAGES_PER_USER} mensajes disponibles."
    )

async def generate_travel_recommendations(travel_request: TravelRequest) -> str:
    """
    Genera recomendaciones personalizadas basadas en la información del viaje
    """
    
    recommendation_prompt = f"""
    Eres un experto en viajes que genera recomendaciones personalizadas.
    
    Información del viaje:
    - Destino: {travel_request.destination}
    - Origen: {travel_request.origin}
    - Pasajeros: {travel_request.passengers}
    - Fechas: {travel_request.departure_date} a {travel_request.return_date}
    - Presupuesto por persona: {travel_request.budget_per_person}€
    
    Genera recomendaciones útiles y específicas sobre:
    1. Mejores barrios/zonas donde alojarse
    2. Actividades imperdibles para esas fechas
    3. Platos típicos que probar
    4. Consejos prácticos de transporte
    5. Estimación de costos (alojamiento, comida, actividades)
    
    Mantén las recomendaciones concisas pero útiles. Usa emojis para hacer el mensaje más visual.
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": recommendation_prompt}
            ],
            temperature=0.8,
            max_tokens=800
        )
        
        content = response.choices[0].message.content
        return content or "No pude generar recomendaciones en este momento."
        
    except Exception as e:
        logging.error(f"Error generando recomendaciones: {e}")
        return "No pude generar recomendaciones en este momento. Pero tu viaje se ve genial! 🎉"

async def admin_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando administrativo para ver información del sistema"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Solo usuarios autorizados pueden usar este comando
    if not is_user_authorized(user_id):
        await update.message.reply_text("❌ Comando no autorizado.")
        return
    
    authorized_users = get_authorized_users_list()
    
    admin_message = "🔧 **Información del Sistema:**\n\n"
    admin_message += f"👥 **Usuarios autorizados:** {len(authorized_users)}\n"
    
    for auth_user in authorized_users:
        messages_used = user_message_count.get(auth_user, 0)
        remaining = MAX_MESSAGES_PER_USER - messages_used
        admin_message += f"• ID `{auth_user}`: {messages_used}/{MAX_MESSAGES_PER_USER} mensajes ({remaining} restantes)\n"
    
    admin_message += f"\n⚙️ **Configuración:**\n"
    admin_message += f"• Límite por usuario: {MAX_MESSAGES_PER_USER} mensajes\n"
    admin_message += f"• Usuarios activos: {len(user_data_store)} con viajes en proceso\n"
    
    await update.message.reply_text(admin_message)

async def quick_tips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Proporciona consejos rápidos de viaje basados en un destino"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Verificar si el usuario está autorizado
    if not is_user_authorized(user_id):
        await update.message.reply_text("❌ Comando no autorizado.")
        return
    
    # Verificar límite de mensajes
    can_send, remaining = can_user_send_message(user_id)
    if not can_send:
        await update.message.reply_text(
            "⚠️ **Límite de mensajes alcanzado**\n\n"
            f"Has alcanzado el límite de {MAX_MESSAGES_PER_USER} mensajes."
        )
        return
    
    # Incrementar contador de mensajes
    increment_message_count(user_id)
    
    # Obtener el destino del comando
    if not update.message.text:
        await update.message.reply_text("❌ Error procesando el comando.")
        return
        
    command_parts = update.message.text.split(" ", 1)
    if len(command_parts) < 2:
        await update.message.reply_text(
            "💡 **Uso:** `/quick_tips [destino]`\n\n"
            "**Ejemplo:** `/quick_tips París`\n\n"
            f"📊 **Mensajes restantes:** {remaining - 1}"
        )
        return
    
    destination = command_parts[1].strip()
    
    # Generar consejos rápidos
    quick_tips_prompt = f"""
    Proporciona 5-7 consejos rápidos y útiles para viajar a {destination}.
    
    Incluye información sobre:
    - Mejor época para visitar
    - Moneda y propinas
    - Transporte público
    - 2-3 atracciones principales
    - Plato típico recomendado
    - Consejo cultural importante
    
    Mantén cada consejo en 1-2 líneas máximo. Usa emojis para hacer el mensaje más visual.
    """
    
    try:
        generating_msg = await update.message.reply_text(f"🔄 Generando consejos para {destination}...")
        
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": quick_tips_prompt}
            ],
            temperature=0.7,
            max_tokens=400
        )
        
        content = response.choices[0].message.content
        
        await generating_msg.delete()
        
        tips_message = f"""
💡 **Consejos rápidos para {destination}:**

{content}

📊 **Mensajes restantes:** {remaining - 1}

¿Quieres planificar un viaje completo? Usa /start 🚀
        """
        
        await update.message.reply_text(tips_message)
        
    except Exception as e:
        await generating_msg.delete()
        logging.error(f"Error generando consejos rápidos: {e}")
        await update.message.reply_text(
            f"❌ No pude generar consejos para {destination}. Inténtalo de nuevo.\n\n"
            f"📊 **Mensajes restantes:** {remaining - 1}"
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra los comandos disponibles"""
    if not update.message:
        return
        
    user_id = update.effective_user.id if update.effective_user else 0
    
    # Verificar si el usuario está autorizado
    if not is_user_authorized(user_id):
        await update.message.reply_text(
            "❌ **Acceso denegado**\n\n"
            "Este bot está restringido a usuarios autorizados.\n"
            f"Tu ID de usuario: `{user_id}`"
        )
        return
        
    help_text = f"""
🤖 **Comandos disponibles:**

/start - Iniciar un nuevo viaje completo
/status - Ver el estado actual de tu viaje y uso de mensajes
/cancel - Cancelar el viaje actual
/quick_tips [destino] - Consejos rápidos para un destino
/help - Mostrar esta ayuda

🔧 **Comandos administrativos:**
/reset_messages - Resetear tu contador de mensajes
/admin_info - Ver información del sistema y usuarios

💡 **Consejos:**
• Puedes escribir de forma natural: "Quiero ir a París desde Madrid"
• Menciona fechas en formato YYYY-MM-DD: "del 15-08-2025 al 22-08-2025"
• Especifica presupuestos: "con 800€ por persona"
• ¡Puedes dar toda la información de una vez o paso a paso!

🚀 **Nuevas funcionalidades:**
• Validación automática de fechas y presupuestos
• Recomendaciones personalizadas al completar viaje
• Consejos rápidos por destino con /quick_tips
• Filtros de seguridad para temas no relacionados

⚠️ **Límites:**
• Máximo {MAX_MESSAGES_PER_USER} mensajes por sesión
• Acceso restringido a usuarios autorizados
• Solo temas relacionados con viajes
    """
    
    await update.message.reply_text(help_text)

def main():
    logging.basicConfig(level=logging.INFO)
    
    # Cargar variables de entorno
    load_dotenv()
    
    token = os.getenv("TRAVEL_BOT_TOKEN")
    
    if not token:
        print("Error: TRAVEL_BOT_TOKEN no encontrado en las variables de entorno")
        return
    
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY no encontrado en las variables de entorno")
        return

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("reset_messages", reset_messages))
    application.add_handler(CommandHandler("admin_info", admin_info))
    application.add_handler(CommandHandler("quick_tips", quick_tips))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot iniciado...")
    application.run_polling()

if __name__ == "__main__":
    main()

