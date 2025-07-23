import random
from threading import Lock

class KeyManager:
    """
    一个通过加权随机轮询方式管理和获取 API 密钥的负载均衡器。

    该类旨在通过非确定性地循环使用一个密钥列表来分配密钥，从而更好地模拟
    多个独立用户的行为，以规避速率限制。它是线程安全的。
    """
    _KEYS = [
        "your_gemini_api_key_1",
        "your_gemini_api_key_2",
        "your_gemini_api_key_3",
        # 可以在这里添加更多的 API 密钥
    ]

    def __init__(self):
        if not self._KEYS:
            raise ValueError("密钥列表不能为空")
        self.keys = self._KEYS
        self.num_keys = len(self.keys)
        # 首次调用时，从一个随机位置开始
        self.counter = random.randint(0, self.num_keys - 1)
        self.lock = Lock()

    def polling_get_key(self):
        """
        通过加权随机步进的轮询算法获取下一个 API 密钥。

        - 首次使用时，从一个随机的密钥开始。
        - 后续调用时，会根据预设的权重随机选择步进值（1, 2, 或 3），
          然后更新计数器以获取下一个密钥。
        - 此方法是线程安全的。

        Returns:
            str: 列表中的下一个 API 密钥。
        """
        with self.lock:
            # 1. 获取当前密钥
            key = self.keys[self.counter]

            # 2. 根据加权概率计算下一个步进值
            # 步进值: [1, 2, 3]
            # 概率:   [50%, 30%, 20%]
            step = random.choices([1, 2, 3], weights=[0.5, 0.3, 0.2], k=1)[0]
            
            # 3. 更新计数器
            self.counter = (self.counter + step) % self.num_keys
            
            return key

    def get_keys(self, n: int):
        """
        根据需要的密钥数量 n，从密钥池中获取一个密钥列表。

        - 如果 n 小于密钥池大小，将进行无放回随机抽样。
        - 如果 n 等于密钥池大小，将返回所有密钥。
        - 如果 n 大于密钥池大小，将进行有放回随机抽样。

        Args:
            n (int): 需要的密钥数量。

        Returns:
            list[str]: 一个包含 n 个密钥的列表。
        """
        if n <= 0:
            return []
        
        with self.lock:
            if n < self.num_keys:
                # 无放回随机抽样
                return random.sample(self.keys, n)
            elif n == self.num_keys:
                # 获取所有密钥
                return self.keys[:]
            else:
                # 有放回随机抽样
                return random.choices(self.keys, k=n)

# 可以创建一个默认的管理器实例供直接导入使用
key_manager = KeyManager()