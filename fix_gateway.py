from pathlib import Path

p = Path("civicpulse/templates/civicpulse-deployments.yaml")
s = p.read_text()

start = s.index('{{- else if eq $key "gateway" }}')
end = s.index('{{- end }}', start)

url = "http" + ":" + "/" + "/"

block = "\n".join([
    '{{- else if eq $key "gateway" }}',
    '          env:',
    '            - name: FRONTEND_CORS_URL_1',
    '              value: "' + url + 'localhost:3000"',
    '            - name: FRONTEND_CORS_URL_2',
    '              value: "' + url + 'localhost:5173"',
    '            - name: AUTH_SERVICE_URL',
    '              value: "' + url + 'auth-service:8081"',
    '            - name: GRIEVANCE_SERVICE_URL',
    '              value: "' + url + 'grievance-service:8082"',
    '            - name: NOTIFICATION_SERVICE_URL',
    '              value: "' + url + 'notification-service:8083"',
]) + "\n"

p.write_text(s[:start] + block + s[end:])
print("Gateway block updated.")
print(block)
