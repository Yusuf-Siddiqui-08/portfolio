INSERT INTO contact_messages (name, email, message, created_at, ip, ua)
VALUES (%s, %s, %s, %s, %s, %s)
RETURNING id;