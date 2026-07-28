StorefrontBot

So i created this whatsapp chatbot, with the intentions of giving out better customer service to bussiness in the sence where by customers dont have to wait long hours for replies.


Overview

StorefrontBot is a Django-based SaaS platform that powers AI-driven WhatsApp chatbots for small and medium-sized businesses. Business owners can onboard to the platform and immediately offer their customers a conversational experience — no coding required on their end.


Features


AI-Powered Replies — Uses Groq's llama-3.3-70b-versatile model to generate contextual, professional responses to customer messages
WhatsApp Integration — Built on Twilio's WhatsApp API (with planned migration to Meta's official API)
Booking Management — Handles appointment bookings with automated daily reminders via APScheduler
Multi-Tenant SaaS — Supports multiple business owners, each with their own storefront bot instance
Admin Dashboard — Powered by Jazzmin for a clean, modern Django admin experience
PostgreSQL Database — Robust data storage for messages, bookings, and business profiles



Tech Stack

LayerTechnologyBackendDjango (Python)DatabasePostgreSQLAI ModelGroq — llama-3.3-70b-versatileWhatsAppTwilio WhatsApp APIAdmin UIJazzminSchedulerAPSchedulerDeploymentRailway


Getting Started

Prerequisites


Python 3.10+
PostgreSQL
A Twilio account with WhatsApp sandbox enabled
A Groq API key


Installation

bash# Clone the repository
git clone https://github.com/FLow1523/StoreFrontBot.git
cd StoreFrontBot

Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

Install dependencies
pip install -r requirements.txt

Environment Variables

Create a .env file in the project root:

envSECRET_KEY=your_django_secret_key
DEBUG=False

DATABASE_URL=your_postgresql_connection_string

GROQ_API_KEY=your_groq_api_key

TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886


Security: Never commit your .env file or expose API keys in your codebase. Rotate any keys that have been made public.



Database Setup

bashpython manage.py migrate
python manage.py createsuperuser

Running Locally

bashpython manage.py runserver


Deployment

StorefrontBot is deployed on Railway. The live instance is available at:

https://web-production-eac2c.up.railway.app

To deploy your own instance on Railway:


Connect your GitHub repository to a new Railway project
Add the required environment variables in the Railway dashboard
Railway will automatically detect the Django app and deploy it



Project Structure

StoreFrontBot/
core/                   Main app — bot logic, message handling
storefront/             Django project settings
templates/             HTML templates
requirements.txt
manage.py
Procfile               Railway/Heroku process definition


Roadmap


 Migrate from Twilio to Meta's official WhatsApp Business API
 Complete daily booking reminder system via APScheduler
 Self-service business onboarding flow
 Analytics dashboard for business owners
 Multi-language support



License

This project is proprietary software. All rights reserved.


Author

Built and maintained by Mathew — Windhoek, Namibia.
