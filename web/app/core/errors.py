"""
중앙 예외 모듈 (errors.py)
=========================
모든 예외를 여기서 정의하고 분류합니다. 앱 전체가 이 모듈의 예외를 사용.

설계 원칙:
1. 모든 예외는 FaxAppError를 상속 → 한 곳에서 잡을 수 있음
2. 각 예외는 성격(category)을 가짐 → 재시도/사용자안내/시스템오류 구분
3. 각 예외는 코드(code)를 가짐 → 로그·통계·추적에 사용
4. 각 예외는 사용자 메시지(user_message)를 가짐 → 화면에 그대로 표시 가능
5. HTTP 상태코드 매핑 → API 응답 자동화

계층 구분:
- ValidationError  : 사용자 입력 잘못 (번호 오타, 빈 파일 등)      → 400, 재시도 무의미
- ConversionError  : 파일 변환 실패                              → 422, 재시도 가능(도구 문제면 시스템)
- FaxServerError   : 팩스 서버 통신 실패                          → 502/503, 재시도 가능
- TransmissionError: 실제 팩스 전송 실패 (회선/상대방)             → 재시도 가능/불가 세분
- SystemError      : 시스템 설정·환경 문제 (도구없음, DB끊김)      → 500, 관리자 필요
- AuthError        : 인증/권한                                    → 401/403
"""
from enum import Enum


class Category(str, Enum):
    """예외 성격 - 대응 방식을 결정."""
    USER = 'user'          # 사용자 잘못 - 고쳐서 다시 시도해야 함
    RETRYABLE = 'retryable'  # 일시적 실패 - 자동 재시도로 해결 가능
    PERMANENT = 'permanent'  # 영구적 실패 - 재시도 무의미
    SYSTEM = 'system'      # 시스템 오류 - 관리자 개입 필요
    AUTH = 'auth'          # 인증/권한


class FaxAppError(Exception):
    """모든 앱 예외의 최상위. 이것만 잡으면 앱 예외 전체를 처리 가능."""
    code = 'UNKNOWN'
    category = Category.SYSTEM
    http_status = 500
    user_message = '알 수 없는 오류가 발생했습니다. 잠시 후 다시 시도해주세요.'
    # 책임 소재: local(우리 서버) / remote(상대방) / link(회선·중간) / user(사용자) / unknown
    fault = 'unknown'

    def __init__(self, detail=None, *, code=None, user_message=None, context=None):
        self.detail = detail or self.user_message
        if code:
            self.code = code
        if user_message:
            self.user_message = user_message
        self.context = context or {}  # 로그용 추가 정보 (job_id, number 등)
        super().__init__(self.detail)

    @property
    def retryable(self):
        return self.category == Category.RETRYABLE

    def to_dict(self):
        """API 응답용."""
        return {
            'error': True,
            'code': self.code,
            'category': self.category.value,
            'fault': self.fault,
            'message': self.user_message,
            'detail': self.detail,
            'retryable': self.retryable,
        }

    def to_log(self):
        """로그용 - context 포함."""
        return {
            'code': self.code, 'category': self.category.value,
            'fault': self.fault,
            'detail': self.detail, 'context': self.context,
        }


# ==================== 입력 검증 ====================
class ValidationError(FaxAppError):
    code = 'VALIDATION'
    fault = 'user'
    category = Category.USER
    http_status = 400
    user_message = '입력값을 확인해주세요.'


class RateLimitError(FaxAppError):
    """발송 요청 한도 초과 (429). 스팸/과금 폭탄 방어."""
    code = 'RATE_LIMIT'
    fault = 'user'
    category = Category.USER
    http_status = 429
    user_message = '요청이 너무 많습니다. 잠시 후 다시 시도해주세요.'

    def __init__(self, retry_after=None, detail=None, context=None):
        self.retry_after = retry_after
        if retry_after:
            self.user_message = f'요청 한도를 초과했습니다. {retry_after}초 후 다시 시도해주세요.'
        super().__init__(detail=detail, context=context)


class SystemError(FaxAppError):
    """시스템 자원 문제 (디스크 부족, 일시적 처리 불가 등).
    fault=system: 우리 쪽 문제. 사용자에겐 '잠시 후' 안내, 관리자 로그 남김."""
    code = 'SYSTEM'
    fault = 'system'
    category = Category.SYSTEM
    http_status = 503
    user_message = '일시적으로 처리할 수 없습니다. 잠시 후 다시 시도해주세요.'

    def __init__(self, detail=None, user_message=None, context=None):
        if user_message:
            self.user_message = user_message
        super().__init__(detail=detail, context=context)


class NotFound(FaxAppError):
    """요청한 리소스를 찾을 수 없음 (404). resource로 대상을 지정."""
    code = 'NOT_FOUND'
    fault = 'user'
    category = Category.USER
    http_status = 404
    user_message = '요청한 항목을 찾을 수 없습니다.'

    def __init__(self, resource=None, detail=None, context=None):
        if resource:
            self.user_message = f'{resource}을(를) 찾을 수 없습니다.'
            ctx = dict(context or {})
            ctx['resource'] = resource
            context = ctx
        super().__init__(detail=detail, context=context)


class Conflict(FaxAppError):
    """리소스 충돌 (409) - 중복 등록 등."""
    code = 'CONFLICT'
    fault = 'user'
    category = Category.USER
    http_status = 409
    user_message = '이미 존재하는 항목입니다.'

    def __init__(self, message=None, detail=None, context=None):
        if message:
            self.user_message = message
        super().__init__(detail=detail, context=context)


class InvalidFaxNumber(ValidationError):
    code = 'INVALID_NUMBER'
    fault = 'user'
    user_message = '팩스번호 형식이 올바르지 않습니다.'


class EmptyFile(ValidationError):
    code = 'EMPTY_FILE'
    fault = 'user'
    user_message = '빈 파일입니다. 내용이 있는 문서를 선택해주세요.'


class UnsupportedFormat(ValidationError):
    code = 'UNSUPPORTED_FORMAT'
    fault = 'user'
    user_message = '지원하지 않는 파일 형식입니다. PDF, Office, 한글, 이미지 파일을 사용해주세요.'


class FileTooLarge(ValidationError):
    code = 'FILE_TOO_LARGE'
    user_message = '파일이 너무 큽니다. 크기를 줄여 다시 시도해주세요.'


class NoRecipients(ValidationError):
    code = 'NO_RECIPIENTS'
    user_message = '받는 번호를 입력해주세요.'


# ==================== 파일 변환 ====================
class ConversionError(FaxAppError):
    code = 'CONVERSION'
    fault = 'local'
    category = Category.RETRYABLE
    http_status = 422
    user_message = '문서를 팩스 형식으로 변환하지 못했습니다.'


class ConversionTimeout(ConversionError):
    code = 'CONVERSION_TIMEOUT'
    fault = 'local'
    user_message = '문서 변환 시간이 초과되었습니다. 페이지 수를 줄여보세요.'


class CorruptDocument(ConversionError):
    code = 'CORRUPT_DOCUMENT'
    category = Category.PERMANENT
    user_message = '문서가 손상되어 변환할 수 없습니다. 파일을 확인해주세요.'


# ==================== 팩스 서버 통신 ====================
class FaxServerError(FaxAppError):
    code = 'FAX_SERVER'
    fault = 'local'
    category = Category.RETRYABLE
    http_status = 502
    user_message = '팩스 서버와 통신하지 못했습니다. 잠시 후 다시 시도됩니다.'


class FaxServerDown(FaxServerError):
    code = 'FAX_SERVER_DOWN'
    fault = 'local'
    http_status = 503
    user_message = '팩스 서버가 응답하지 않습니다. 관리자에게 문의해주세요.'


class FaxServerTimeout(FaxServerError):
    code = 'FAX_SERVER_TIMEOUT'
    fault = 'local'
    user_message = '팩스 서버 응답이 지연되고 있습니다. 잠시 후 다시 시도됩니다.'


class QueueFull(FaxServerError):
    code = 'QUEUE_FULL'
    fault = 'local'
    user_message = '발송 대기열이 가득 찼습니다. 잠시 후 다시 시도해주세요.'


# ==================== 실제 전송 (회선/상대방) ====================
class TransmissionError(FaxAppError):
    code = 'TRANSMISSION'
    category = Category.RETRYABLE
    http_status = 200  # 발송 요청 자체는 접수됨, 결과가 실패
    user_message = '팩스 전송에 실패했습니다.'


class LineBusy(TransmissionError):
    code = 'LINE_BUSY'
    fault = 'remote'
    user_message = '상대방 회선이 통화 중입니다. 잠시 후 재시도됩니다.'


class NoAnswer(TransmissionError):
    code = 'NO_ANSWER'
    fault = 'remote'
    user_message = '상대방이 응답하지 않습니다. 재시도됩니다.'


class NumberUnreachable(TransmissionError):
    code = 'NUMBER_UNREACHABLE'
    fault = 'remote'
    category = Category.PERMANENT
    user_message = '연결할 수 없는 번호입니다. 번호를 확인해주세요.'


class NegotiationFailed(TransmissionError):
    code = 'NEGOTIATION_FAILED'
    fault = 'link'
    user_message = '팩스 규격 협상에 실패했습니다(상대 팩스기 문제일 수 있음). 재시도됩니다.'


class TransmissionInterrupted(TransmissionError):
    code = 'TX_INTERRUPTED'
    fault = 'link'
    user_message = '전송 중 연결이 끊겼습니다. 재시도됩니다.'


class PartialTransmission(TransmissionError):
    code = 'PARTIAL_TX'
    fault = 'link'
    category = Category.RETRYABLE
    user_message = '일부 페이지만 전송되었습니다. 재시도됩니다.'

    def __init__(self, sent_pages=None, total_pages=None, detail=None, context=None):
        # 몇 페이지 중 몇 페이지 전송됐는지 메시지에 반영
        if sent_pages is not None and total_pages is not None:
            self.user_message = (f'{total_pages}페이지 중 {sent_pages}페이지만 '
                                 f'전송되었습니다. 재시도됩니다.')
            ctx = dict(context or {})
            ctx.update({'sent_pages': sent_pages, 'total_pages': total_pages})
            context = ctx
        super().__init__(detail=detail, context=context)


# ── T.30 프로토콜 표준 오류 (spandsp T30_ERR_* 대응) ──
class FaxTimeout(TransmissionError):
    """T.30 타이머 만료 (T0/T1/T2/T3/T5). 대개 상대 팩스기 지연·무응답."""
    code = 'FAX_TIMEOUT'
    fault = 'link'
    category = Category.RETRYABLE
    user_message = '상대 팩스기 응답 대기 중 시간이 초과되었습니다. 재시도됩니다.'


class TrainingFailed(TransmissionError):
    """모뎀 트레이닝/속도 협상 실패. 회선 품질 문제일 수 있음."""
    code = 'TRAINING_FAILED'
    fault = 'link'
    category = Category.RETRYABLE
    user_message = '회선 속도 협상에 실패했습니다(회선 품질 문제). 재시도됩니다.'


class RemoteIncompatible(TransmissionError):
    """상대 팩스기가 호환되지 않음/수신 불가. 영구 실패."""
    code = 'REMOTE_INCOMPATIBLE'
    fault = 'remote'
    category = Category.PERMANENT
    user_message = '상대 팩스기와 호환되지 않거나 수신할 수 없습니다.'


class EcmError(TransmissionError):
    """ECM(오류정정) 관련 실패. 재시도 가치 있음."""
    code = 'ECM_ERROR'
    fault = 'link'
    category = Category.RETRYABLE
    user_message = '오류정정(ECM) 처리에 실패했습니다. 재시도됩니다.'


class CarrierLost(TransmissionError):
    """전송 중 반송파 상실(회선 끊김). 재시도."""
    code = 'CARRIER_LOST'
    fault = 'link'
    category = Category.RETRYABLE
    user_message = '전송 중 회선 신호가 끊겼습니다. 재시도됩니다.'


class ProtocolError(TransmissionError):
    """예기치 않은 T.30 메시지/프레임 순서 오류. 재시도."""
    code = 'PROTOCOL_ERROR'
    fault = 'link'
    category = Category.RETRYABLE
    user_message = '팩스 프로토콜 오류가 발생했습니다. 재시도됩니다.'


class BadTiff(ConversionError):
    """TIFF/F 파일 문제(형식·페이지·태그). 문서 자체 문제 → 영구."""
    code = 'BAD_TIFF'
    fault = 'local'
    category = Category.PERMANENT
    user_message = '팩스 이미지 파일에 문제가 있습니다. 문서를 확인해주세요.'


class CallDropped(TransmissionError):
    """호가 조기에 끊김. 재시도."""
    code = 'CALL_DROPPED'
    fault = 'link'
    category = Category.RETRYABLE
    user_message = '통화가 예기치 않게 끊겼습니다. 재시도됩니다.'


# ==================== 시스템 ====================
class SystemConfigError(FaxAppError):
    code = 'SYSTEM_CONFIG'
    category = Category.SYSTEM
    http_status = 500
    user_message = '시스템 설정 오류입니다. 관리자에게 문의해주세요.'


class ToolNotInstalled(SystemConfigError):
    code = 'TOOL_NOT_INSTALLED'
    fault = 'local'
    user_message = '서버에 필요한 변환 도구가 설치되지 않았습니다. 관리자에게 문의해주세요.'


class DatabaseError(FaxAppError):
    code = 'DATABASE'
    category = Category.SYSTEM
    http_status = 503
    user_message = '데이터베이스 연결에 문제가 있습니다. 잠시 후 다시 시도해주세요.'


# ==================== 인증/권한 ====================
class AuthError(FaxAppError):
    code = 'AUTH'
    fault = 'user'
    category = Category.AUTH
    http_status = 401
    user_message = '로그인이 필요합니다.'


class SessionExpired(AuthError):
    code = 'SESSION_EXPIRED'
    fault = 'user'
    user_message = '세션이 만료되었습니다. 다시 로그인해주세요.'


class PermissionDenied(AuthError):
    code = 'PERMISSION_DENIED'
    fault = 'user'
    http_status = 403
    user_message = '이 작업을 수행할 권한이 없습니다.'


# ==================== 팩스 서버 에러코드 → 예외 매핑 ====================
# 팩스 서버가 돌려주는 결과 문자열을 앱 예외로 변환.
# 실제 faxserver_v6.py / XMS의 응답 코드에 맞춰 조정.
_SERVER_RESULT_MAP = {
    'busy': LineBusy,
    'no_answer': NoAnswer,
    'noanswer': NoAnswer,
    'no answer': NoAnswer,
    'unreachable': NumberUnreachable,
    'invalid_number': NumberUnreachable,
    'no_route': NumberUnreachable,
    'negotiation': NegotiationFailed,
    'training_failed': NegotiationFailed,
    'protocol_error': NegotiationFailed,
    'interrupted': TransmissionInterrupted,
    'hangup': TransmissionInterrupted,
    'partial': PartialTransmission,
    'partial_page': PartialTransmission,
    'page_mismatch': PartialTransmission,
    'incomplete_pages': PartialTransmission,
    'timeout': FaxServerTimeout,
    'queue_full': QueueFull,

    # ── spandsp T.30 표준 오류 코드 (원본 소스 t30.h 기준) ──
    # 타이머 만료류 → 재시도
    't30_err_cedtone': FaxTimeout,
    't30_err_t0_expired': FaxTimeout,
    't30_err_t1_expired': FaxTimeout,
    't30_err_t3_expired': FaxTimeout,
    't30_err_rx_t2expdcn': FaxTimeout,
    't30_err_rx_t2expd': FaxTimeout,
    't30_err_rx_t2expfax': FaxTimeout,
    't30_err_rx_t2expmps': FaxTimeout,
    't30_err_rx_t2exprr': FaxTimeout,
    't30_err_rx_t2exp': FaxTimeout,
    't30_err_tx_t5exp': FaxTimeout,
    't30_err_rx_nofax': FaxTimeout,
    't30_err_rx_noeol': FaxTimeout,
    # 트레이닝/협상 실패 → 재시도
    't30_err_cannot_train': TrainingFailed,
    't30_err_hdlc_carrier': TrainingFailed,
    't30_err_tx_baddcs': NegotiationFailed,
    't30_err_tx_badpg': NegotiationFailed,
    't30_err_tx_invalrsp': NegotiationFailed,
    't30_err_tx_nodis': NegotiationFailed,
    't30_err_tx_phbdead': NegotiationFailed,
    't30_err_tx_phddead': NegotiationFailed,
    # 원격 비호환/수신불가 → 영구 실패
    't30_err_incompatible': RemoteIncompatible,
    't30_err_rx_incapable': RemoteIncompatible,
    't30_err_tx_incapable': RemoteIncompatible,
    't30_err_noressupport': RemoteIncompatible,
    't30_err_nosizesupport': RemoteIncompatible,
    # ECM 관련 → 재시도
    't30_err_tx_ecmphd': EcmError,
    't30_err_rx_ecmphd': EcmError,
    # 반송파 상실/호 끊김 → 재시도
    't30_err_rx_nocarrier': CarrierLost,
    't30_err_calldropped': CallDropped,
    't30_err_tx_gotdcn': CallDropped,
    't30_err_rx_gotdcs': CallDropped,
    't30_err_retrydcn': CallDropped,
    # 프로토콜/예기치 않은 메시지 → 재시도
    't30_err_unexpected': ProtocolError,
    't30_err_rx_invalcmd': ProtocolError,
    't30_err_oper_int_fail': ProtocolError,
    't30_err_rx_dcnwhy': ProtocolError,
    't30_err_rx_dcndata': ProtocolError,
    't30_err_rx_dcnfax': ProtocolError,
    't30_err_rx_dcnphd': ProtocolError,
    't30_err_rx_dcnrrd': ProtocolError,
    't30_err_rx_dcnnortn': ProtocolError,
    # TIFF 파일 문제 → 영구 (문서 자체 문제)
    't30_err_fileerror': BadTiff,
    't30_err_nopage': BadTiff,
    't30_err_badtiff': BadTiff,
    't30_err_badpage': BadTiff,
    't30_err_badtag': BadTiff,
    't30_err_badtiffhdr': BadTiff,
    't30_err_nomem': BadTiff,
    # 간략 키워드로도 매칭되게 (부분 일치 대비)
    'cannot_train': TrainingFailed,
    'incompatible': RemoteIncompatible,
    'nocarrier': CarrierLost,
    'calldropped': CallDropped,
    'ecm': EcmError,
    'badtiff': BadTiff,

    # ── FreeSWITCH/spandsp가 주는 자연어 실패 텍스트 (부분 일치) ──
    'timed out waiting': FaxTimeout,
    'not compatible': RemoteIncompatible,
    'not able to receive': RemoteIncompatible,
    'not able to transmit': RemoteIncompatible,
    'carrier lost': CarrierLost,
    'unexpected message': ProtocolError,
    'invalid ecm': EcmError,
    'failed to train': TrainingFailed,
    'call dropped': CallDropped,
    'disconnected after': CallDropped,
    'tiff': BadTiff,
    'negotiation failed': NegotiationFailed,
    't.38 negotiation': NegotiationFailed,

    # ── Q.850 Hangup Cause (FreeSWITCH가 주는 끊김 원인) ──
    'normal_temporary_failure': FaxServerTimeout,   # 일시적 실패 → 재시도
    'normal_clearing': TransmissionInterrupted,     # 정상 종료지만 팩스 미완 시
    'user_busy': LineBusy,                          # 통화중
    'no_user_response': NoAnswer,                   # 무응답
    'no_answer': NoAnswer,
    'call_rejected': NumberUnreachable,             # 거부
    'number_changed': NumberUnreachable,
    'unallocated_number': NumberUnreachable,        # 없는 번호
    'invalid_number_format': NumberUnreachable,
    'destination_out_of_order': NumberUnreachable,
    'network_out_of_order': FaxServerTimeout,        # 망 장애 → 재시도
    'normal_unspecified': TransmissionError,
    'recovery_on_timer_expire': FaxTimeout,         # 타이머 만료
    'media_timeout': FaxTimeout,
    'incompatible_destination': RemoteIncompatible,  # 상대 비호환
    'requested_chan_unavail': FaxServerTimeout,      # 채널 부족 → 재시도
    'switch_congestion': FaxServerTimeout,           # 혼잡 → 재시도
    'gateway_down': FaxServerDown,
}


def from_server_result(result_text, context=None):
    """
    팩스 서버 결과 문자열을 앱 예외로 변환.
    매핑 안 되면 일반 TransmissionError.
    """
    if not result_text:
        return TransmissionError(context=context)
    key = str(result_text).strip().lower().replace('-', '_')
    exc_cls = _SERVER_RESULT_MAP.get(key)
    if not exc_cls:
        # 부분 일치 시도 - 더 긴(구체적인) 키를 우선
        for k in sorted(_SERVER_RESULT_MAP, key=len, reverse=True):
            if k in key:
                exc_cls = _SERVER_RESULT_MAP[k]
                break
    exc_cls = exc_cls or TransmissionError
    return exc_cls(detail=f'팩스 서버 결과: {result_text}', context=context)


def classify_result(result_text):
    """
    결과 문자열로 최종 상태를 판정: 'sent' | 'failed' | 'retry'
    동기화·재시도 로직에서 사용.
    """
    if not result_text:
        return 'retry'
    key = str(result_text).strip().lower()
    if key in ('sent', 'success', 'ok', 'delivered', 'completed'):
        return 'sent'
    try:
        exc = from_server_result(result_text)
        if exc.category == Category.PERMANENT:
            return 'failed'
        if exc.category == Category.RETRYABLE:
            return 'retry'
    except Exception:
        pass
    return 'failed'


# ── 실패 코드 → fault(책임소재) 매핑 (통계/리포트용) ──
def _build_code_fault_map():
    """모든 FaxAppError 하위 클래스를 순회해 code→fault 매핑 자동 생성."""
    mapping = {}
    def walk(cls):
        for sub in cls.__subclasses__():
            code = getattr(sub, 'code', None)
            fault = getattr(sub, 'fault', 'unknown')
            if code:
                mapping[code] = fault
            walk(sub)
    walk(FaxAppError)
    return mapping

CODE_FAULT_MAP = _build_code_fault_map()

# fault 한글 라벨 (리포트 표시용)
FAULT_LABELS = {
    'user': '사용자',      # 잘못된 번호·파일 등 발신자 측 문제
    'local': '우리 시스템', # 변환·팩스서버 등 우리 측 문제
    'remote': '상대 팩스',  # 상대 팩스기 비호환·미응답
    'link': '회선',        # 회선 사용중·끊김·통신 품질
    'unknown': '알 수 없음',
}

def fault_of(code):
    """실패 코드의 책임소재(fault)를 반환. 모르면 'unknown'."""
    if not code:
        return 'unknown'
    return CODE_FAULT_MAP.get(str(code).upper(), 'unknown')

def fault_label(fault):
    """fault 코드의 한글 라벨."""
    return FAULT_LABELS.get(fault, fault)
