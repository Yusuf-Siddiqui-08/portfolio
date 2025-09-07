import resend

resend.api_key = "re_RNzjscZL_5rkRrEuqvh5f1egTdzRjqCGg"

params: resend.Emails.SendParams = {
        "from": "Yusuf's Portfolio <onboarding@resend.dev>",
        "to": ["yusufs98783@gmail.com"],
        "subject": "hello world",
        "html": "<strong>it works!</strong>",
    }

r = resend.Emails.send(params)
print(r)