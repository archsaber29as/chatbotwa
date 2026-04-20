#!/bin/bash
   echo "$GOOGLE_CREDENTIALS_B64" | base64 -d > credentials.json
   echo "$GOOGLE_TOKEN_B64" | base64 -d > token.pickle
   python app.py