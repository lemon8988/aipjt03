// 백엔드 주소. 로컬에서는 localhost, 배포(Netlify)에서는 Render 에 올린 백엔드 주소를 쓴다.
// 배포 후 아래 PROD_API 를 실제 백엔드 URL 로 바꾼다. (끝에 / 없이)
const PROD_API = "https://REPLACE-WITH-YOUR-BACKEND.onrender.com";
const isLocal = ["localhost", "127.0.0.1"].includes(location.hostname);
window.API_BASE = isLocal ? "http://localhost:8000" : PROD_API;
