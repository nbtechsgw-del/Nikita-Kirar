# Online Tourism Management System

This project is a Python-based web application for managing tourism packages,
hotel booking, customer records, package bookings, payments, feedback, and admin
reports.

## Technologies Used

| Technology | Purpose |
| --- | --- |
| Python | Backend programming language |
| Flask | Web framework for routes, templates, sessions, and forms |
| SQLite | Local development database |
| MySQL | Recommended production database |
| HTML/CSS | Frontend page structure and styling |
| JavaScript | Optional client-side validation and UI enhancements |
| Bootstrap or custom CSS | Responsive user interface |
| Gunicorn / Waitress | Production deployment server |

## Main Modules

- Admin module: package management, user list, booking approval, revenue reports.
- User module: registration, login, package search, booking history, feedback.
- Tour package module: domestic and international packages, pricing, duration,
  itinerary, availability, and offers.
- Hotel module: hotel listing, room pricing, availability, and ratings.
- Booking module: package booking, confirmation status, cancellation status, and
  invoice-style booking records.
- Payment module: transaction ID generation, payment method, and payment status.
- Feedback module: ratings, reviews, and customer suggestions.

## How To Run

```bash
cd online_Tourism_management_system
python app.py
```

Open `http://127.0.0.1:5000` in a browser.

Default admin login:

```text
Email: admin@tourism.local
Password: admin123
```

The app creates `tourism.db` automatically on first run.
