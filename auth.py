import os
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

LOGIN_URL = "https://gapbrands.r-pac.com.hk/Account/Login"

def get_authenticated_session() -> requests.Session:
    """
    Authenticates with the GAP Brands portal and returns a configured requests.Session.
    """
    username = os.getenv("GAP_USERNAME", "GAP0012")
    password = os.getenv("GAP_PASSWORD")
    
    if not password:
        raise ValueError("GAP_PASSWORD environment variable is not set.")

    session = requests.Session()
    
    # Step 1: GET the login page to extract the anti-forgery token
    response = session.get(LOGIN_URL)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, "html.parser")
    token_input = soup.find("input", {"name": "__RequestVerificationToken"})
    
    if not token_input:
        raise ValueError("Could not find __RequestVerificationToken on the login page.")
        
    token = token_input.get("value")
    
    # Step 2: POST the login form
    login_data = {
        "ReturnUrl": "",
        "ReturnUrlHash": "",
        "LoginInput.UserNameOrEmailAddress": username,
        "LoginInput.Password": password,
        "LoginInput.RememberMe": "true",
        "__RequestVerificationToken": token
    }
    
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://gapbrands.r-pac.com.hk",
        "Referer": "https://gapbrands.r-pac.com.hk/Account/Login"
    }
    
    post_response = session.post(LOGIN_URL, data=login_data, headers=headers)
    post_response.raise_for_status()
    
    # Check if login was successful
    # Usually the login page is returned again if it fails
    if 'name="__RequestVerificationToken"' in post_response.text and "Login" in post_response.text:
        raise ValueError("Login failed! Please check your GAP_USERNAME and GAP_PASSWORD in the .env file.")
        
    # Set default headers for all future API requests so they return 401 instead of 302 redirects
    session.headers.update({
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest"
    })
    
    return session

