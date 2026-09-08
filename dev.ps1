# Windows equivalent of `make dev`: starts backend and frontend in two child windows.
$root = $PSScriptRoot
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\backend'; uv run uvicorn app.main:app --host 127.0.0.1 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root\frontend'; npm run dev"
Write-Host "backend  -> http://127.0.0.1:8000/api/status"
Write-Host "frontend -> http://localhost:5173"
