"""
VAPID 키 생성 스크립트
실행: python generate_vapid_keys.py
생성된 키를 .env 파일에 추가하세요.
"""
from py_vapid import Vapid
import base64

vapid = Vapid()
vapid.generate_keys()

# Private key (PEM → base64url 단축 형태)
raw_private = vapid.private_key.private_numbers().private_value
private_bytes = raw_private.to_bytes(32, byteorder='big')
private_key_b64 = base64.urlsafe_b64encode(private_bytes).decode('utf-8').rstrip('=')

# Public key (uncompressed point → base64url)
public_numbers = vapid.private_key.public_key().public_numbers()
x_bytes = public_numbers.x.to_bytes(32, byteorder='big')
y_bytes = public_numbers.y.to_bytes(32, byteorder='big')
public_key_bytes = b'\x04' + x_bytes + y_bytes
public_key_b64 = base64.urlsafe_b64encode(public_key_bytes).decode('utf-8').rstrip('=')

print("=" * 60)
print("VAPID 키가 생성되었습니다!")
print("아래 내용을 .env 파일에 추가하세요:")
print("=" * 60)
print(f"VAPID_PRIVATE_KEY={private_key_b64}")
print(f"VAPID_PUBLIC_KEY={public_key_b64}")
print("=" * 60)
