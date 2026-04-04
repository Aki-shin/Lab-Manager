import os
from app import create_app
from dotenv import load_dotenv

load_dotenv()

app = create_app()

if __name__ == '__main__':
    port = int(os.getenv('MANAGER_PORT', 80))
    # host='0.0.0.0' делает доступным в локальной сети
    app.run(host='0.0.0.0', port=port, debug=True)