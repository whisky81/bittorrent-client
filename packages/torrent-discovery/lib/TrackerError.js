
class TrackerError extends Error {
    constructor(message, event, scheme, url, code) {
        super(message);
        this.event = event;
        this.scheme = scheme;
        this.url = url;
        this.code = code;
        this.name = this.constructor.name;
        Error.captureStackTrace(this, this.constructor);
    }
}

export default TrackerError;