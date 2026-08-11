import pandas as pd
from prophet import Prophet
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. Connect to your Google Sheet (just like your web app does)
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("/Users/varun/Documents/PythonCoding/Project 1/cloud_key.json", scope)
client = gspread.authorize(creds)
sheet = client.open("Weather Database").sheet1

# 2. Get the records and turn them into a DataFrame
records = sheet.get_all_records()
df = pd.DataFrame(records)

# 3. Format the columns for Prophet
df = df[['Timestamp', 'Temperature']]
df.columns = ['ds', 'y']
df['ds'] = pd.to_datetime(df['ds'])

# 4. Train the model
model = Prophet()
model.fit(df)

# 5. Predict the next 2 hours (8 periods of 15 minutes)
future = model.make_future_dataframe(periods=8, freq='15min')
forecast = model.predict(future)

# Print the future predictions
print(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].head(5))
print(forecast[['ds', 'yhat', 'yhat_lower', 'yhat_upper']].tail(5))