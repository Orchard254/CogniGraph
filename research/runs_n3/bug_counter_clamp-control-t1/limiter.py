class Limiter:
    def clamp(self, value):
        return max(0, min(value, 100))
