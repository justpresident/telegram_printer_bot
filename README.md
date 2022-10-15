# Telegram printer bot
This is a simple bot that allows you to print on your home printer from anywhere from any device that has a telegram client.
It also exposes some basic printer diagnostics and print job management.
In order to print something, just upload a file without any commands. A lot of formats are supported.

## Commands:
 * `/start` - Starts interaction and print current state
 * `/auth <password>` - Authenticates current session with a password against password stored in auth_password file
 * `/pending` - Lists all pending print jobs
 * `/completed` - Lists last 10 completed print jobs
 * `/cancel <job_id>` - Cancels a pending job with a given id 
 
## Details
 * Uses simple password authentication
 * Uses libreoffice to convert files to PDF format before printing
 * Uses `lpr` to print send a file on your default printer
 * Uses python-telegram-bot package to interface with the Telegram API.

# Install

* Python packages: `pip3 install python-telegram-bot`
* Libreoffice. Install it using your package manager, like 'apt install libreoffice'
