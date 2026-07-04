# Plex Play AI 업스케일 서버

Ubuntu Plex 서버의 Intel GPU에서 Vulkan 기반 Real-ESRGAN을 실행합니다. 영상은
실시간 변환하지 않습니다. 앱이 처음 요청하면 서버가 백그라운드에서 2배 확대
영상을 만들고, 완성된 다음 재생부터 자동으로 AI 영상을 사용합니다.

## 설치

Intel 그래픽 드라이버와 Plex Media Server가 설치된 Ubuntu에서 실행합니다.

```bash
cd ai-upscale-server
sudo bash install-ubuntu.sh
```

설치기는 Plex의 `Preferences.xml`에서 토큰을 자동으로 읽고, 공식
Real-ESRGAN NCNN Vulkan Ubuntu 실행 파일과 FFmpeg를 설치합니다.

상태 확인:

```bash
systemctl status plex-ai-upscale --no-pager
curl http://127.0.0.1:32600/health
```

앱과 Plex 서버가 같은 내부망에 있어야 합니다. UFW를 사용하는 경우 앱이 있는
내부망에서 TCP 32600 포트로 접근할 수 있게 허용합니다. 인터넷 전체에 32600
포트를 개방하지 마세요.

앱의 `재생 설정 → AI 업스케일 → 사용`을 선택하면 Plex 서버 주소와 동일한
호스트의 32600 포트를 자동으로 사용합니다. 별도 서버 주소나 토큰을 앱에 입력할
필요가 없습니다.

## 동작과 저장공간

- 자연 영상용 `realesrgan-x4plus` 모델과 2배 확대를 사용합니다.
- 결과 해상도는 최대 3840×2160으로 제한합니다.
- 한 번에 영상 하나를 처리해 Intel 내장 GPU의 과부하를 줄입니다.
- 기본 캐시 한도는 100GB이며 오래 사용하지 않은 결과부터 자동 삭제합니다.
- 처리 로그는 `/var/lib/plex-ai-upscale/<ratingKey>.log`에 저장합니다.

설정은 `/etc/plex-ai-upscale.env`에서 바꿀 수 있습니다. 변경 후 다음 명령으로
적용합니다.

```bash
sudo systemctl restart plex-ai-upscale
```
