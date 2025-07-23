import os
import json
import glob
import re
import shutil
from typing import List, Dict, Any, Optional, Set
from google import genai
from google.genai import types
from pathlib import Path
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import PyPDF2
import threading

# 自己定义的模块
from keys import key_manager

# 配置代理 (如果需要)
os.environ['http_proxy'] = 'http://127.0.0.1:7897'
os.environ['https_proxy'] = 'http://127.0.0.1:7897'


class DirectoryScanner:
    """目录结构扫描器"""
    
    def scan_pdf_structure(self, root_dir: str) -> Dict[str, List[str]]:
        """
        扫描目录结构，返回按相对路径组织的PDF文件字典
        
        Returns:
            Dict[相对路径, PDF文件名列表]
        """
        pdf_structure = {}
        total_pdfs = 0
        
        for root, dirs, files in os.walk(root_dir):
            rel_path = os.path.relpath(root, root_dir)
            if rel_path == '.':
                rel_path = ''
            
            pdf_files = [f for f in files if f.lower().endswith('.pdf')]
            if pdf_files:
                pdf_structure[rel_path] = pdf_files
                total_pdfs += len(pdf_files)
        
        logging.info(f"📁 扫描完成: 发现 {total_pdfs} 个PDF文件，分布在 {len(pdf_structure)} 个目录中")
        return pdf_structure
    
    def get_all_pdf_relative_paths(self, pdf_structure: Dict[str, List[str]]) -> List[str]:
        """获取所有PDF的相对路径列表"""
        all_pdfs = []
        for rel_dir, pdf_files in pdf_structure.items():
            for pdf_file in pdf_files:
                if rel_dir:
                    pdf_rel_path = os.path.join(rel_dir, pdf_file).replace('\\', '/')
                else:
                    pdf_rel_path = pdf_file
                all_pdfs.append(pdf_rel_path)
        return all_pdfs


class DirectoryManager:
    """目录管理器"""
    
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.base_name = Path(base_dir).name
        
    def create_output_structure(self) -> Dict[str, str]:
        """创建输出目录结构"""
        return {
            'jsonl': f"{self.base_name}_jsonl",
            'slices': f"{self.base_name}_pdf_slices",
            'logs': "log"
        }
    
    def mirror_directory_structure(self, pdf_structure: Dict[str, List[str]], target_base: str):
        """镜像目录结构到目标目录"""
        Path(target_base).mkdir(parents=True, exist_ok=True)
        
        for rel_path in pdf_structure.keys():
            if rel_path:  # 跳过根目录
                target_path = Path(target_base) / rel_path
                target_path.mkdir(parents=True, exist_ok=True)
        
        logging.info(f"📂 已创建镜像目录结构: {target_base}")


class ProcessingStateManager:
    """处理状态管理器"""
    
    def __init__(self, pdf_directory: str):
        self.base_dir = pdf_directory
        self.base_name = Path(pdf_directory).name
        self.completed_list_path = f"{self.base_name}_已完美处理的pdf列表.txt"
        self.retry_list_path = f"{self.base_name}_需重试&未处理的pdf列表.txt"
        self.fail_log_path = "log/agent_fail_info.log"
        
        # 确保log目录存在
        Path("log").mkdir(exist_ok=True)
    
    def load_completed_pdfs(self) -> Set[str]:
        """加载已完美处理的PDF列表"""
        if not os.path.exists(self.completed_list_path):
            return set()
        
        try:
            with open(self.completed_list_path, 'r', encoding='utf-8') as f:
                completed = {line.strip() for line in f if line.strip()}
            logging.info(f"📋 加载已完成列表: {len(completed)} 个PDF")
            return completed
        except Exception as e:
            logging.warning(f"⚠️  加载已完成列表失败: {e}")
            return set()
    
    def load_retry_pdfs(self) -> Set[str]:
        """加载需重试的PDF列表"""
        if not os.path.exists(self.retry_list_path):
            return set()
        
        try:
            with open(self.retry_list_path, 'r', encoding='utf-8') as f:
                retry_pdfs = {line.strip() for line in f if line.strip()}
            logging.info(f"📋 加载重试列表: {len(retry_pdfs)} 个PDF")
            return retry_pdfs
        except Exception as e:
            logging.warning(f"⚠️  加载重试列表失败: {e}")
            return set()
    
    def save_completed_pdfs(self, completed_pdfs: Set[str]):
        """保存已完美处理的PDF列表"""
        try:
            with open(self.completed_list_path, 'w', encoding='utf-8') as f:
                for pdf_path in sorted(completed_pdfs):
                    f.write(f"{pdf_path}\n")
        except Exception as e:
            logging.error(f"❌ 保存已完成列表失败: {e}")
    
    def save_retry_pdfs(self, retry_pdfs: Set[str]):
        """保存需重试的PDF列表"""
        try:
            with open(self.retry_list_path, 'w', encoding='utf-8') as f:
                for pdf_path in sorted(retry_pdfs):
                    f.write(f"{pdf_path}\n")
        except Exception as e:
            logging.error(f"❌ 保存重试列表失败: {e}")
    
    def mark_as_completed(self, pdf_rel_path: str, completed_pdfs: Set[str], retry_pdfs: Set[str]):
        """标记PDF为已完美处理"""
        completed_pdfs.add(pdf_rel_path)
        retry_pdfs.discard(pdf_rel_path)
        self.save_completed_pdfs(completed_pdfs)
        self.save_retry_pdfs(retry_pdfs)
        logging.info(f"✅ 标记为已完成: {pdf_rel_path}")
    
    def mark_as_failed(self, pdf_rel_path: str, error_info: str, retry_pdfs: Set[str]):
        """标记PDF处理失败"""
        retry_pdfs.add(pdf_rel_path)
        self.save_retry_pdfs(retry_pdfs)
        
        # 记录到失败日志
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self.fail_log_path, 'a', encoding='utf-8') as f:
                f.write(f"[{timestamp}] FAILED: {pdf_rel_path}\n")
                f.write(f"Error: {error_info}\n")
                f.write("="*80 + "\n")
        except Exception as e:
            logging.error(f"❌ 写入失败日志失败: {e}")
        
        logging.error(f"❌ 标记为失败: {pdf_rel_path}")
    
    def get_pending_pdfs(self, all_pdfs: List[str]) -> List[str]:
        """获取需要处理的PDF列表"""
        completed_pdfs = self.load_completed_pdfs()
        retry_pdfs = self.load_retry_pdfs()
        
        # 需要处理的PDF = 所有PDF - 已完成的PDF
        pending_pdfs = []
        for pdf_path in all_pdfs:
            if pdf_path not in completed_pdfs:
                pending_pdfs.append(pdf_path)
                # 如果不在重试列表中，添加到重试列表
                if pdf_path not in retry_pdfs:
                    retry_pdfs.add(pdf_path)
        
        # 保存更新的重试列表
        self.save_retry_pdfs(retry_pdfs)
        
        logging.info(f"📊 处理状态统计:")
        logging.info(f"   总PDF数量: {len(all_pdfs)}")
        logging.info(f"   已完成: {len(completed_pdfs)}")
        logging.info(f"   待处理: {len(pending_pdfs)}")
        
        return pending_pdfs


class SliceManager:
    """切片管理器"""
    
    def cleanup_pdf_slices(self, pdf_rel_path: str, slices_base_dir: str):
        """清空指定PDF的切片目录"""
        pdf_name = Path(pdf_rel_path).stem
        pdf_parent_dir = Path(pdf_rel_path).parent
        
        slice_dir = Path(slices_base_dir) / pdf_parent_dir / pdf_name
        
        if slice_dir.exists():
            try:
                shutil.rmtree(slice_dir)
                logging.info(f"🧹 已清空切片目录: {slice_dir}")
            except Exception as e:
                logging.warning(f"⚠️  清空切片目录失败 {slice_dir}: {e}")


class GeminiAPIManager:
    """统一的Gemini API请求管理器，提供完整的日志记录和重试机制"""
    
    def __init__(self, default_max_retries: int = 3):
        self.default_max_retries = default_max_retries
        self._thread_local = threading.local()
    
    def _get_client(self) -> genai.Client:
        """获取线程本地的客户端实例"""
        if not hasattr(self._thread_local, 'client'):
            api_key = key_manager.polling_get_key()
            self._thread_local.client = genai.Client(api_key=api_key)
        return self._thread_local.client
    
    def _refresh_client_with_new_key(self):
        """强制刷新客户端并使用新的API key"""
        api_key = key_manager.polling_get_key()
        self._thread_local.client = genai.Client(api_key=api_key)
        logging.info(f"🔄 已切换到新的API Key: ...{api_key[-8:]}")
    
    def _extract_status_code(self, exception: Exception) -> Optional[int]:
        """从异常消息中提取HTTP状态码"""
        error_message = str(exception)
        
        # 尝试多种模式匹配状态码
        patterns = [
            r'(\d{3})\s+\w+',  # "503 UNAVAILABLE"
            r'status_code:\s*(\d{3})',  # "status_code: 503"
            r'HTTP\s+(\d{3})',  # "HTTP 503"
            r'(\d{3})\s+Service',  # "503 Service Unavailable"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, error_message)
            if match:
                try:
                    return int(match.group(1))
                except (ValueError, IndexError):
                    continue
        
        return None
    
    def _should_retry_with_new_key(self, status_code: Optional[int]) -> bool:
        """判断是否应该更换key重试"""
        if status_code is None:
            return False
        return status_code in [429, 503]  # 速率限制和服务过载
    
    def _should_retry(self, exception: Exception, status_code: Optional[int]) -> bool:
        """判断是否应该重试"""
        # 明确不应重试的状态码
        if status_code in [400, 403, 404]:
            return False
        
        # 明确应该重试的状态码
        if status_code in [429, 500, 503, 504]:
            return True
        
        # 对于网络相关异常，也应该重试
        exception_name = type(exception).__name__
        network_exceptions = [
            'RemoteProtocolError',
            'ConnectTimeout', 
            'ReadTimeout',
            'ConnectionError',
            'TimeoutError',
            'OSError',  # 包含网络相关的OS错误
        ]
        
        if exception_name in network_exceptions:
            return True
        
        # 检查异常消息中的关键词
        error_message = str(exception).lower()
        network_keywords = [
            'timeout',
            'connection',
            'disconnected',
            'network',
            'unreachable',
            'refused'
        ]
        
        for keyword in network_keywords:
            if keyword in error_message:
                return True
        
        # 默认重试（保守策略）
        return True
    
    def _log_request(self, request_name: str, model_name: str, prompt: str, config: Dict[str, Any], max_retries: int):
        """记录请求日志"""
        request_log = {
            "request_name": request_name,
            "model": model_name,
            "prompt": prompt,
            "config": config,
            "max_retries": max_retries,
            "thread_id": threading.current_thread().ident
        }
        
        logging.debug(f"=== API Request [{request_name}] ===\n{json.dumps(request_log, indent=2, ensure_ascii=False)}")
        logging.info(f"🚀 Starting API call: {request_name}")
    
    def _log_response(self, request_name: str, response: genai.types.GenerateContentResponse, attempt: int):
        """记录响应日志"""
        try:
            response_text = response.text
            logging.debug(f"=== API Response [{request_name}] (Attempt {attempt}) ===\n{response_text}")
            logging.info(f"✅ API call successful: {request_name} (Attempt {attempt})")
        except Exception as e:
            logging.warning(f"⚠️  Could not log response for {request_name}: {e}")
    
    def _log_retry(self, request_name: str, attempt: int, max_retries: int, error: Exception, wait_time: float, status_code: Optional[int], will_change_key: bool):
        """记录重试日志"""
        error_type = type(error).__name__
        error_msg = str(error)
        
        # 状态码信息
        status_info = f"HTTP {status_code}" if status_code else "Network/Unknown"
        
        # 重试策略说明
        strategy = "🔑 Change Key + Retry" if will_change_key else "🔄 Retry"
        
        logging.warning(f"{strategy} {attempt}/{max_retries} for {request_name}")
        logging.warning(f"   Status: {status_info}")
        logging.warning(f"   Error Type: {error_type}")
        logging.warning(f"   Error Message: {error_msg}")
        logging.info(f"⏳ Waiting {wait_time:.1f}s before retry...")
    
    def _log_final_failure(self, request_name: str, max_retries: int, error: Exception, status_code: Optional[int]):
        """记录最终失败日志"""
        status_info = f"HTTP {status_code}" if status_code else "Network/Unknown"
        logging.error(f"❌ API call failed permanently for {request_name} after {max_retries} attempts")
        logging.error(f"   Final Status: {status_info}")
        logging.error(f"   Final Error: {type(error).__name__}: {error}")
    
    def make_request(
        self, 
        request_name: str, 
        model_name: str, 
        contents: List[Any], 
        config: Dict[str, Any], 
        max_retries: Optional[int] = None
    ) -> genai.types.GenerateContentResponse:
        """
        统一的API请求方法，包含完整的日志记录和重试机制
        
        Args:
            request_name: 请求的描述性名称，用于日志记录
            model_name: Gemini模型名称
            contents: 请求内容列表
            config: 请求配置
            max_retries: 最大重试次数，默认使用类的默认值
            
        Returns:
            API响应对象
            
        Raises:
            Exception: 当所有重试都失败时抛出最后一个异常
        """
        max_retries = max_retries or self.default_max_retries
        
        # 获取prompt用于日志
        prompt = ""
        for content in contents:
            if isinstance(content, str):
                prompt = content
                break
        
        # 记录请求开始
        self._log_request(request_name, model_name, prompt, config, max_retries)
        
        last_exception = None
        
        for attempt in range(1, max_retries + 1):
            try:
                client = self._get_client()
                
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config
                )
                
                # 记录成功响应
                self._log_response(request_name, response, attempt)
                return response
                
            except Exception as e:
                last_exception = e
                
                # 提取状态码
                status_code = self._extract_status_code(e)
                
                # 判断是否应该重试
                if not self._should_retry(e, status_code):
                    error_info = f"HTTP {status_code}" if status_code else type(e).__name__
                    logging.error(f"💥 Non-retryable error for {request_name}: {error_info}: {e}")
                    raise
                
                # 如果不是最后一次尝试，进行重试
                if attempt < max_retries:
                    # 判断是否需要更换key
                    will_change_key = self._should_retry_with_new_key(status_code)
                    
                    # 计算等待时间（指数退避）
                    wait_time = min(4 ** (attempt - 1), 60)  # 最大等待60秒
                    
                    # 记录重试信息
                    self._log_retry(request_name, attempt, max_retries, e, wait_time, status_code, will_change_key)
                    
                    # 如果需要，更换API key
                    if will_change_key:
                        try:
                            self._refresh_client_with_new_key()
                        except Exception as key_error:
                            logging.warning(f"⚠️  Failed to refresh API key: {key_error}")
                    
                    # 等待后重试
                    time.sleep(wait_time)
                else:
                    # 最后一次尝试失败
                    self._log_final_failure(request_name, max_retries, e, status_code)
        
        # 所有重试都失败，抛出最后一个异常
        raise last_exception


class PlannerSelector:
    """阶段一：模板规划器"""
    
    def __init__(self, template_dir: str, api_manager: GeminiAPIManager):
        self.api_manager = api_manager
        self.templates = self._load_templates(template_dir)
        logging.info(f"✅ 成功加载 {len(self.templates)} 个V2模板")

    def _load_templates(self, template_dir: str) -> Dict[str, Any]:
        templates = {}
        template_files = glob.glob(os.path.join(template_dir, "*.json"))
        for file_path in template_files:
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    template_data = json.load(f)
                    template_name = template_data.get("template_name")
                    if template_name:
                        templates[template_name] = template_data
                except json.JSONDecodeError:
                    logging.warning(f"⚠️  无法解析模板文件 {file_path}，已跳过")
        return templates

    def _get_planning_materials(self) -> (str, Dict[str, Any]):
        template_json_list = [json.dumps(template, ensure_ascii=False) for template in self.templates.values()]
        available_templates_str = "\n---\n".join(template_json_list)
        prompt = f"""
        你是一个专业的法律文档结构分析师。你的核心任务是：
        1.  **分析文档**: 完整分析给定的PDF文档，理解其宏观结构、内容类型和层级深度。
        2.  **选择模板**: 从下方提供的"可用模板列表"中，为该文档选择一个或多个最合适的模板。
        3.  **确定层级**: 对于你选择的每个模板，精确分析其对应内容在文档中的最大结构层级深度（1到5之间）。
        **核心指导原则**: 你在选择模板时，应**主要分析该文件的结构，次要分析该文件属于什么法律类型，因为法律类型影响结构但不一定决定结构**。
        可用模板列表:
        ---
        {available_templates_str}
        ---
        请严格按照下面提供的JSON Schema格式输出你的分析结果。
        """
        schema = {
            "type": "object", "properties": {
                "selections": {
                    "type": "array", "description": "为文档选择的模板列表", "items": {
                        "type": "object", "properties": {
                            "template_name": {"type": "string", "description": "从可用模板列表中选择的模板名称"},
                            "reason": {"type": "string", "description": "选择此模板的简要理由"},
                            "max_hierarchy_level": {"type": "integer", "description": "分析该模板所对应的内容后，确定其结构所需的最大层级深度（1-5）。"}
                        }, "required": ["template_name", "reason", "max_hierarchy_level"]
                    }
                }
            }, "required": ["selections"]
        }
        return prompt, schema

    def _get_refinement_materials(self, original_template: Dict[str, Any], max_level: int) -> str:
        """为模板优化步骤准备Prompt"""
        original_template_str = json.dumps(original_template, ensure_ascii=False, indent=2)
        
        prompt = f"""
        你是一个专业的法律文档提取模板优化师。你的任务是接收一个原始模板和一个PDF文档，然后对模板进行优化和修正，使其完美适配文档并符合API规范。

        **任务指令:**

        1.  **API兼容性修正 (最重要)**:
            *   仔细检查下方提供的"原始模板"中的 `extraction_schema`。
            *   **硬性规则**: Google Gemini API **不支持** `additionalProperties` 字段。如果原始模板中包含此字段，你**必须**将其修正。
            *   **修正策略**: 对于用于表格数据的对象，应将其转换为一个**对象数组**。数组中的每个对象代表一个单元格，并包含两个固定的键：`"column_name"` (字符串，列名) 和 `"cell_value"` (字符串，单元格内容)。同时，你必须同步更新 `extraction_prompt` 来解释这个新的数据结构。

        2.  **层级修剪**:
            *   分析 `extraction_schema` 中的 `hierarchy` 字段。
            *   根据给定的最大层级深度 `{max_level}`，你必须从 `hierarchy` 的 `properties` 中移除所有更深的层级定义。例如，如果 `max_level` 是 2, 你必须移除 `p3`, `p4`, `p5`。

        3.  **内容驱动优化 (可选)**:
            *   快速浏览PDF内容，如果能识别出关键的、重复的结构（如表格的列名），可以在 `extraction_prompt` 中加入简短示例，以提高提取精度。

        4.  **保持完整性与简约性**:
            *   **严禁引入**原始模板 `extraction_schema` 中未曾使用过的JSON Schema特性（如 `pattern`, `format`, `anyOf` 等）。你的任务是**修正和删减**，而不是增加新的复杂性。
            *   除了上述修正外，模板的其余部分应保持原样。
            *   你最终的输出必须是一个**完整、有效、可直接使用**的JSON对象，代表优化后的模板。

        ---
        **最大层级深度 (max_hierarchy_level):**
        {max_level}
        ---
        **原始模板:**
        ```json
        {original_template_str}
        ```
        ---

        请根据以上所有指令，分析给定的PDF文档，并输出优化后的完整模板JSON对象。确保你的输出是一个可以被Python的`json.loads()`成功解析的字符串。
        """
        return prompt

    def plan(self, pdf_path: str) -> Dict[str, Any]:
        logging.info("🎯 Phase 1.1: Template Selection Starting...")
        selection_prompt, selection_schema = self._get_planning_materials()
        
        try:
            pdf_data = Path(pdf_path).read_bytes()

            # === 步骤 1: 选择最合适的模板 ===
            selection_response = self.api_manager.make_request(
                request_name="Phase1.1_Template_Selection",
                model_name='gemini-2.5-flash',
                contents=[selection_prompt, types.Part.from_bytes(data=pdf_data, mime_type='application/pdf')],
                config={"response_mime_type": "application/json", "response_schema": selection_schema}
            )
            
            raw_selection = json.loads(selection_response.text)

            # === 步骤 2: 对选中的每个模板进行优化 ===
            logging.info("🎯 Phase 1.2: Template Refinement Starting...")
            precisioned_templates = []
            
            for i, selection in enumerate(raw_selection.get("selections", []), 1):
                template_name = selection.get("template_name")
                max_level = selection.get("max_hierarchy_level", 5)
                original_template = self.templates.get(template_name)

                if not original_template:
                    logging.warning(f"⚠️  AI选择了不存在的模板 '{template_name}'，已跳过")
                    continue

                logging.info(f"🔧 Refining template '{template_name}' ({i}/{len(raw_selection.get('selections', []))}) with max_level={max_level}")
                
                refinement_prompt = self._get_refinement_materials(original_template, max_level)

                # 调用统一的API管理器进行优化
                refinement_response = self.api_manager.make_request(
                    request_name=f"Phase1.2_Template_Refinement_{template_name}",
                    model_name='gemini-2.5-flash',
                    contents=[refinement_prompt, types.Part.from_bytes(data=pdf_data, mime_type='application/pdf')],
                    config={"response_mime_type": "application/json"}
                )
                
                # === 客户端校验与修复 ===
                try:
                    refined_template = json.loads(refinement_response.text)
                except json.JSONDecodeError:
                    logging.warning(f"⚠️  模板 '{template_name}' 优化返回无效JSON，已跳过")
                    continue

                # 确保extraction_schema是对象而非字符串
                extractor_config = refined_template.get("extractor_config", {})
                if isinstance(extractor_config.get("extraction_schema"), str):
                    try:
                        extractor_config["extraction_schema"] = json.loads(extractor_config["extraction_schema"])
                    except json.JSONDecodeError:
                        logging.warning(f"⚠️  无法解析模板 '{template_name}' 的stringified extraction_schema，已跳过")
                        continue

                # 使用优化后的模板
                final_template = {
                    "template_name": f"{template_name}_precisioned_for_{Path(pdf_path).stem}",
                    "description": refined_template.get("description"),
                    "target_document_type": refined_template.get("target_document_type"),
                    "extraction_prompt": refined_template.get("extractor_config", {}).get("extraction_prompt"),
                    "extraction_schema": refined_template.get("extractor_config", {}).get("extraction_schema")
                }
                precisioned_templates.append(final_template)

            logging.info(f"✅ Phase 1 完成，生成 {len(precisioned_templates)} 个精确化模板")
            return {"source_pdf_path": pdf_path, "precisioned_templates": precisioned_templates}

        except Exception as e:
            logging.error(f"❌ Phase 1 执行失败", exc_info=True)
            return {"source_pdf_path": pdf_path, "precisioned_templates": []}


class PlannerPartitioner:
    """阶段二：任务规划器"""
    
    def __init__(self, api_manager: GeminiAPIManager):
        self.api_manager = api_manager
    
    def _get_planning_materials(self, precisioned_template_set: Dict[str, Any]) -> (str, Dict[str, Any]):
        templates = precisioned_template_set.get("precisioned_templates", [])
        
        # 生成带序号的模板描述列表
        numbered_templates_str = ""
        for i, t in enumerate(templates, 1):
            # 包含完整的模板JSON，并用序号标记
            template_json_str = json.dumps(t, ensure_ascii=False, indent=2)
            numbered_templates_str += f"模板 {i}:\n```json\n{template_json_str}\n```\n---\n"
            
        # 根据模板数量调整提示
        if len(templates) == 1:
            assignment_instruction = "4.  **分配模板序号**: 本次只有一个可用模板，请为所有任务的 `assigned_template_index` 字段指定为 1。"
        else:
            assignment_instruction = "4.  **分配模板序号**: 为每个子任务，从下方提供的\"可用模板列表\"中选择最匹配的一个，并指定其**序号**（从1开始）。"

        prompt = f"""
        你是一个专业的PDF任务调度员。你的核心任务是：

        1.  **确保完整覆盖**: 你的首要目标是确保生成的任务列表能够**完整覆盖PDF从第一页到最后一页的所有内容**。必须特别注意处理文档的封面、目录、发布令、前言等所有"非正文"部分，不能有任何遗漏。
        2.  **智能任务切分**: 将一个给定的PDF文档，根据其逻辑结构（章节、条款、表格等），分解为一个或多个需要提取的子任务（Chunks）。
        3.  **划定提取边界**: 为每个子任务，不仅要定义其处理的`page_range`（页码范围），还必须生成一段精确的`extraction_scope_description`（文字描述）来定义提取的逻辑边界。
        {assignment_instruction}

        **反面示例（你必须避免的行为）:**
        - **错误做法**: 假设文档的第7、8、9页内容连续。你**不应该**将它们拆分为 `page_range: [7,7]`, `[8,8]`, `[9,9]` 三个独立的任务。
        - **正确做法**: 你**必须**将它们合并成一个任务，例如 `page_range: [7,9]`，并相应地描述 `extraction_scope_description`。
        - **决定禁止的行为**: 你**绝对禁止**一直将任务粒度切分到单页级别，除非是处理特殊的内容（如单页表格或单页条款或者处理跨页边界的补救措施），否则每个任务的 `page_range` **必须至少包含2页，理想大小是3到5页**。

        **非常重要的硬性约束**: 你生成的每个子任务的 `page_range` **跨度绝对不能超过6页，理想大小是3到5页**。如果一个逻辑章节（例如"第二章"）本身超过了6页，你必须将其分解为多个子任务（例如"第二章前半部分"、"第二章后半部分"），并通过 `extraction_scope_description` 来确保提取的连续性。

        **处理跨页边界的特殊指令与注意力引导**:

        *   **核心原则：预见性重叠**: 你的规划应具备预见性。在切分任务时，应提前观察逻辑章节的结束位置。我们强烈推荐你主动使用页码重叠来避免尴尬的"断头"任务。
            *   **推荐做法**: 假设一个章节从55页开始，到62页上半部分结束。最优的切分不是 `[55, 61]` 和 `[62, 62]`，而是更有远见的 `[55, 58]` 和 `[58, 62]`。这展示了你优秀的注意力广度和规划能力。

        *   **关键原则：章节过渡页的强制重叠**: 当一个页面同时包含上一个章节的结尾和下一个新章节的开头时，你必须应用此重叠策略。
            *   **场景示例**: 假设上一个任务为 `page_range: [59, 62]`，其描述为"提取第十二章的全部内容"。但你观察到第62页在结束第十二章后，紧接着开始了第十三章。
            *   **强制操作**: 在这种情况下，你的下一个任务的 `page_range` **必须从 `[62, ...]` 开始**。例如 `[62, 65]`。其 `extraction_scope_description` 应为"从第62页开始，提取第十三章的全部内容"。这确保了两个任务在第62页上重叠，从而完整地捕获了两个章节的边界，没有任何遗漏。

        *   **补救措施：强制边界子任务**: 如果你因为某种原因（如达到了6页的上限）已经输出了一个不包含结尾页的子任务（例如，上一个任务是 `[55, 61]`，但第62页还有属于第十二章节的内容），那么你 **必须** 在下个子任务中补救这个遗漏。
            *   **补救示例**: 在上述情况下，你必须马上在下个任务中创建一个 `page_range` 为 `[62, 62]` 的新任务，其 `extraction_scope_description` 应精确描述为："请仅提取第62页中，属于第十二章的所有内容" 或 "请仅提取第62页中，在新章节标题出现之前的所有内容"。

        *   **最终校验**: 在生成所有任务后，你必须根据pdf在内部进行一次最终检查，确保所有任务的 `extraction_scope_description` 在逻辑上是首尾相连的，没有任何条款或内容的遗漏。
        可用模板列表:
        ---
        {numbered_templates_str}
        请严格按照下面提供的JSON Schema格式输出你的分析结果。
        """
        schema = {
            "type": "object", "properties": {
                "tasks": {
                    "type": "array", "description": "为文档规划的子任务列表", "items": {
                        "type": "object", "properties": {
                            "task_id": {"type": "string", "description": "子任务的唯一ID，建议格式为 'pdf_name_chunk_N'"},
                            "page_range": {"type": "array", "items": {"type": "integer"}, "description": "该任务在PDF中的起始和结束页码"},
                            "extraction_scope_description": {"type": "string", "description": "对提取范围的精确文字描述"},
                            "assigned_template_index": {"type": "integer", "description": "为此任务分配的精确化模板的序号 (从1开始)"}
                        }, "required": ["task_id", "page_range", "extraction_scope_description", "assigned_template_index"]
                    }
                }
            }, "required": ["tasks"]
        }
        return prompt, schema

    def _build_execution_plan(self, raw_tasks: Dict[str, Any], pdf_path: str, precisioned_template_set: Dict[str, Any]) -> List[Dict[str, Any]]:
        execution_plan = []
        templates_list = precisioned_template_set.get("precisioned_templates", [])
        
        if not templates_list:
            logging.error("❌ 在构建执行计划时，没有任何可用的精确化模板。")
            return []

        for task_data in raw_tasks.get("tasks", []):
            task_id = task_data.get('task_id')
            template_index = task_data.get("assigned_template_index") # 1-based index

            if template_index is None:
                logging.warning(f"⚠️  AI为任务 '{task_id}' 返回的响应中缺少 'assigned_template_index'，已跳过")
                continue

            # 将1-based的序号转换为0-based的列表索引
            template_real_index = template_index - 1

            if not (0 <= template_real_index < len(templates_list)):
                logging.warning(f"⚠️  AI为任务 '{task_id}' 分配了无效的模板序号 '{template_index}'，有效范围是 1-{len(templates_list)}，已跳过")
                continue
            
            assigned_template = templates_list[template_real_index]
            
            execution_plan.append({
                "task_id": task_id,
                "source_pdf_path": pdf_path,
                "physical_pdf_slice_path": None,
                "page_range": task_data.get("page_range"),
                "extraction_scope_description": task_data.get("extraction_scope_description"),
                "assigned_template": assigned_template
            })
        return execution_plan

    def plan(self, pdf_path: str, precisioned_template_set: Dict[str, Any]) -> List[Dict[str, Any]]:
        logging.info("🎯 Phase 2: Task Partitioning Starting...")
        prompt, schema = self._get_planning_materials(precisioned_template_set)
        try:
            pdf_data = Path(pdf_path).read_bytes()
            
            response = self.api_manager.make_request(
                request_name="Phase2_Task_Partitioning",
                model_name='gemini-2.5-flash',
                contents=[prompt, types.Part.from_bytes(data=pdf_data, mime_type='application/pdf')],
                config={"response_mime_type": "application/json", "response_schema": schema}
            )
            
            raw_tasks = json.loads(response.text)
            execution_plan = self._build_execution_plan(raw_tasks, pdf_path, precisioned_template_set)
            logging.info(f"✅ Phase 2 完成，生成 {len(execution_plan)} 个执行任务")
            return execution_plan
        except Exception as e:
            logging.error(f"❌ Phase 2 执行失败", exc_info=True)
            return []


class PDFSlicer:
    """阶段2.1：物理PDF切片器"""
    def slice_and_update_plan(self, execution_plan: List[Dict[str, Any]], pdf_path: str, slices_base_dir: str) -> List[Dict[str, Any]]:
        logging.info("🔪 Phase 2.1: PDF Slicing Starting...")
        
        # 计算切片目录路径
        pdf_rel_path = os.path.relpath(pdf_path, self.original_pdf_directory)
        pdf_name = Path(pdf_path).stem
        pdf_parent_dir = Path(pdf_rel_path).parent
        slice_dir = Path(slices_base_dir) / pdf_parent_dir / pdf_name
        slice_dir.mkdir(parents=True, exist_ok=True)

        try:
            with open(pdf_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                for i, task in enumerate(execution_plan, 1):
                    task_id = task.get("task_id")
                    page_range = task.get("page_range")
                    if not task_id or not page_range or len(page_range) != 2:
                        logging.warning(f"⚠️  任务 {task_id} 数据无效，跳过切片")
                        continue

                    start_page, end_page = page_range[0] - 1, page_range[1] - 1 # 页码从1开始，索引从0开始
                    writer = PyPDF2.PdfWriter()
                    for j in range(start_page, end_page + 1):
                        if 0 <= j < len(reader.pages):
                            writer.add_page(reader.pages[j])
                        else:
                            logging.warning(f"⚠️  页码 {j+1} 超出PDF范围，已跳过")
                    
                    if len(writer.pages) > 0:
                        slice_filename = f"{task_id}.pdf"
                        slice_path = slice_dir / slice_filename
                        with open(slice_path, 'wb') as output_pdf:
                            writer.write(output_pdf)
                        task["physical_pdf_slice_path"] = str(slice_path)
                        logging.debug(f"✂️  Created slice {i}/{len(execution_plan)}: {slice_path}")
                    else:
                        logging.warning(f"⚠️  任务 {task_id} 无有效页面，未设置物理切片路径")

            logging.info(f"✅ Phase 2.1 完成，成功切片 {len([t for t in execution_plan if t.get('physical_pdf_slice_path')])} 个任务")
        except Exception as e:
            logging.error(f"❌ PDF切片过程失败", exc_info=True)
            # 如果切片失败，将所有路径设置为空
            for task in execution_plan:
                task["physical_pdf_slice_path"] = None
        
        return execution_plan

    def set_original_pdf_directory(self, pdf_directory: str):
        """设置原始PDF目录，用于计算相对路径"""
        self.original_pdf_directory = pdf_directory


class PlannerRepartitioner:
    """阶段2.5：智能重分割器"""
    
    def __init__(self, api_manager: GeminiAPIManager):
        self.api_manager = api_manager
    
    def _get_repartition_materials(self, failed_task: Dict[str, Any]) -> (str, Dict[str, Any]):
        original_id = failed_task.get('task_id')
        original_pages = failed_task.get('page_range')
        original_desc = failed_task.get('extraction_scope_description')

        prompt = f"""
        你是一个专业的任务分割专家。一个处理PDF的任务因为内容过多或过于复杂而失败了，你需要将其智能地分割成两个更小的、逻辑连贯的子任务。
    
        **失败的任务信息如下：**
        - **原始任务ID**: `{original_id}`
        - **原始页码范围**: `{original_pages}`
        - **原始提取目标**: `{original_desc}`
    
        **你的任务是：**
        1.  **分析内容**: 基于原始的提取目标，分析这个页码范围内的内容结构。
        2.  **寻找最佳分割点**: 在页码范围的中间位置附近，找到一个最合理的逻辑分割点（例如，一个章节的末尾，一个条款的结束，或一个表格的中间）。
        3.  **生成两个子任务**: 创建两个新的子任务定义。
    
        **硬性要求与指导原则**:
        - **允许页码重叠**: 为了确保逻辑的完整性，两个新子任务的页码范围**可以重叠**。例如，如果原始范围是 `[10, 16]`，一个好的分割可能是 `[10, 13]` 和 `[13, 16]`，其中第13页被两个任务共享，以确保该页上的任何过渡内容都被完整捕获。
        - **完整覆盖**: 两个新子任务的页码范围组合起来必须能完整覆盖原始的页码范围。
        - **精确的描述**: 必须为每个子任务生成一个**全新的**、精确描述其提取范围的 `extraction_scope_description`。例如，不要只说"第一部分"，而要说"从第10页开始，提取到第13页中间的'XX条'结束之前的所有内容"。
        - **新的任务ID**: 每个子任务都需要有一个新的、唯一的 `task_id` (在原始ID后加上 `_sub_1`, `_sub_2` 等后缀)。
    
        请严格按照下面提供的JSON Schema格式输出你的分析结果。
        """
        schema = {
            "type": "object", "properties": {
                "sub_tasks": {
                    "type": "array", "description": "分割后的子任务列表", "items": {
                        "type": "object", "properties": {
                            "task_id": {"type": "string", "description": "新子任务的唯一ID"},
                            "page_range": {"type": "array", "items": {"type": "integer"}, "description": "新子任务的页码范围"},
                            "extraction_scope_description": {"type": "string", "description": "对新子任务提取范围的精确文字描述"}
                        }, "required": ["task_id", "page_range", "extraction_scope_description"]
                    }
                }
            }, "required": ["sub_tasks"]
        }
        return prompt, schema

    def repartition(self, failed_task: Dict[str, Any]) -> List[Dict[str, Any]]:
        task_id = failed_task.get('task_id')
        pdf_path = failed_task.get('source_pdf_path')
        logging.info(f"🔀 Phase 2.5: Smart-splitting task {task_id}")
        prompt, schema = self._get_repartition_materials(failed_task)
        try:
            pdf_data = Path(pdf_path).read_bytes()
            
            response = self.api_manager.make_request(
                request_name=f"Phase2.5_Smart_Split_{task_id}",
                model_name='gemini-2.5-flash',
                contents=[prompt, types.Part.from_bytes(data=pdf_data, mime_type='application/pdf')],
                config={"response_mime_type": "application/json", "response_schema": schema}
            )
            
            result_data = json.loads(response.text)
            sub_tasks = result_data.get("sub_tasks", [])
            logging.info(f"✅ Smart-split for {task_id} 生成 {len(sub_tasks)} 个子任务")
            return sub_tasks
        except Exception as e:
            logging.error(f"❌ Smart-split任务 {task_id} 执行失败", exc_info=True)
            return []


class Extractor:
    """阶段三：知识提取执行器"""

    COMMON_EXTRACTION_RULES = """
    \n**通用提取规则**:
    - **最高优先级规则**: 绝对禁止提取任何页眉、页脚、页码、水印或与法律正文无关的出版商/数据库/二维码信息（例如'威科先行'、'Wolters Kluwer'、'扫一扫,手机阅读更方便'等）。
    - **绝对保真**: 必须100%保留原文，严禁任何形式的总结、缩写或改写。
    - **输出格式**: 必须严格按照提供的JSON Schema格式化输出。
    - **动态容错**: 如果你预测本次提取的JSON输出会非常长，以至于超出最大Token限制，你必须在返回的JSON中增加一个顶级键值对 `"status": "INCOMPLETE_OUTPUT_MAX_TOKENS"`。这是最高优先级的指令，绝对不能忘记。不要尝试缩减内容，保持原文的完整性。
    """

    def __init__(self, api_manager: GeminiAPIManager):
        self.api_manager = api_manager

    def _preprocess_schema_types(self, schema_node: Any) -> Any:
        """递归地将schema中所有'type'字段的值转换为大写，以兼容google-genai SDK"""
        if isinstance(schema_node, dict):
            new_dict = {}
            for key, value in schema_node.items():
                if key == "type" and isinstance(value, str):
                    new_dict[key] = value.upper()
                else:
                    new_dict[key] = self._preprocess_schema_types(value)
            return new_dict
        elif isinstance(schema_node, list):
            return [self._preprocess_schema_types(item) for item in schema_node]
        else:
            return schema_node

    def extract(self, task: Dict[str, Any], max_retries: int = 3) -> Dict[str, Any]:
        """执行单个提取任务"""
        task_id = task.get("task_id", "N/A")
        base_prompt = task.get("assigned_template", {}).get("extraction_prompt", "")
        scope_description = task.get("extraction_scope_description", "")
        raw_schema = task.get("assigned_template", {}).get("extraction_schema", {})
        
        # 预处理schema以确保类型为大写
        schema = self._preprocess_schema_types(raw_schema)
        
        # 优先使用物理切片，如果不存在则回退到原始PDF
        pdf_to_use = task.get("physical_pdf_slice_path") or task.get("source_pdf_path")

        if not all([base_prompt, scope_description, schema, pdf_to_use]):
            logging.error(f"❌ 任务 {task_id} 缺少必要字段")
            return {"task_id": task_id, "status": "ERROR", "error_message": "任务对象缺少必要字段", "knowledge_units": []}
        
        final_prompt = f"重要指令：{scope_description}\n\n以下是具体的提取要求：\n{base_prompt}{self.COMMON_EXTRACTION_RULES}"
        
        try:
            logging.info(f"🏃 Executing task: {task_id} using PDF: {Path(pdf_to_use).name}")
            
            pdf_data = Path(pdf_to_use).read_bytes()
            pdf_part = types.Part.from_bytes(data=pdf_data, mime_type='application/pdf')
            
            request_config = {
                "response_mime_type": "application/json",
                "response_schema": schema
            }

            # 使用统一的API管理器
            response = self.api_manager.make_request(
                request_name=f"Phase3_Knowledge_Extraction_{task_id}",
                model_name="gemini-2.5-flash",
                contents=[final_prompt, pdf_part],
                config=request_config,
                max_retries=max_retries
            )

            if not response.candidates or not response.candidates[0].content.parts:
                finish_reason = response.prompt_feedback.block_reason if response.prompt_feedback else "Unknown"
                error_message = f"API调用成功但返回空内容. Block Reason: {finish_reason}"
                logging.warning(f"⚠️  任务 {task_id} 警告: {error_message}")
                return {"task_id": task_id, "status": "NO_CONTENT", "error_message": error_message, "knowledge_units": []}
            
            result_data = json.loads(response.text)
            knowledge_units = result_data.get("knowledge_units", [])

            # 智能页码校正
            if task.get("physical_pdf_slice_path") and task.get("page_range"):
                start_page, end_page = task.get("page_range")
                logging.debug(f"🔧 Running page correction for {task_id} with range [{start_page}, {end_page}]")
                
                for unit in knowledge_units:
                    reported_page = unit.get("page_number")
                    if reported_page is None:
                        continue

                    if not (start_page <= reported_page <= end_page):
                        corrected_page = reported_page + start_page - 1
                        if start_page <= corrected_page <= end_page:
                            logging.debug(f"📄 页码校正: {reported_page} -> {corrected_page} for item '{unit.get('item_id')}'")
                            unit["page_number"] = corrected_page
                        else:
                            logging.warning(f"⚠️  页码校正失败: item '{unit.get('item_id')}', 报告页码:{reported_page}, 校正后:{corrected_page}, 范围:[{start_page}, {end_page}]")
            
            logging.info(f"✅ 任务 {task_id} 成功提取 {len(knowledge_units)} 个知识单元")
            return {"task_id": task_id, "status": "SUCCESS", "error_message": None, "knowledge_units": knowledge_units}

        except Exception as e:
            logging.error(f"❌ 任务 {task_id} 执行失败", exc_info=True)
            return {"task_id": task_id, "status": "ERROR", "error_message": str(e), "knowledge_units": []}


def merge_results(results: List[Dict[str, Any]], output_path: str):
    """合并所有任务结果"""
    logging.info("📝 Merging results...")
    all_units = []
    for res in results:
        if res.get("status") == "SUCCESS":
            all_units.extend(res.get("knowledge_units", []))
    
    # 确保输出目录存在
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        for unit in all_units:
            f.write(json.dumps(unit, ensure_ascii=False) + '\n')
    logging.info(f"✅ 成功合并 {len(all_units)} 个知识单元到 {output_path}")


class PDFRAGAgentV2:
    """PDF-RAG V2 Agent 主控制流"""
    def __init__(self, template_dir: str = "extraction_templates_v2"):
        # 创建统一的API管理器
        self.api_manager = GeminiAPIManager(default_max_retries=3)
        
        # 初始化所有组件
        self.planner_selector = PlannerSelector(template_dir, self.api_manager)
        self.planner_partitioner = PlannerPartitioner(self.api_manager)
        self.pdf_slicer = PDFSlicer()
        self.planner_repartitioner = PlannerRepartitioner(self.api_manager)
        self.extractor = Extractor(self.api_manager)
        self.slice_manager = SliceManager()

    def process_single_pdf(self, pdf_rel_path: str, pdf_full_path: str, output_dirs: Dict[str, str], 
                          state_manager: ProcessingStateManager, completed_pdfs: Set[str], 
                          retry_pdfs: Set[str], total_count: int, completed_count: int):
        """处理单个PDF文件"""
        
        # 显示进度
        percentage = (completed_count / total_count) * 100 if total_count > 0 else 0
        logging.info(f"📊 进度: {completed_count}/{total_count} ({percentage:.1f}%) - 当前: {pdf_rel_path}")
        
        # 清空该PDF的切片目录
        self.slice_manager.cleanup_pdf_slices(pdf_rel_path, output_dirs['slices'])
        
        logging.info(f"🚀 === 开始处理PDF: {pdf_rel_path} ===")
        
        try:
            # Phase 1: Template Selection & Refinement
            precisioned_template_set = self.planner_selector.plan(pdf_full_path)
            if not precisioned_template_set.get("precisioned_templates"):
                error_msg = "Phase 1 失败：模板选择和精炼失败"
                logging.error(f"❌ {error_msg}")
                state_manager.mark_as_failed(pdf_rel_path, error_msg, retry_pdfs)
                return
                
            # Phase 2: Task Partitioning
            execution_plan = self.planner_partitioner.plan(pdf_full_path, precisioned_template_set)
            if not execution_plan:
                error_msg = "Phase 2 失败：任务分割失败"
                logging.error(f"❌ {error_msg}")
                state_manager.mark_as_failed(pdf_rel_path, error_msg, retry_pdfs)
                return

            # Phase 2.1: PDF Slicing
            self.pdf_slicer.set_original_pdf_directory(self.original_pdf_directory)
            execution_plan = self.pdf_slicer.slice_and_update_plan(execution_plan, pdf_full_path, output_dirs['slices'])

            # Phase 3: Concurrent Knowledge Extraction
            num_tasks = len(execution_plan)
            MAX_WORKERS = min(num_tasks, 8)
            logging.info(f"🏃‍♂️ Phase 3: 并发执行 {num_tasks} 个任务，使用 {MAX_WORKERS} 个工作线程")
            
            results_map = {}
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_task = {executor.submit(self.extractor.extract, task): task for task in execution_plan}
                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    task_id = task.get('task_id')
                    try:
                        results_map[task_id] = future.result()
                    except Exception as exc:
                        logging.error(f"❌ 任务 {task_id} 产生未处理异常", exc_info=True)
                        results_map[task_id] = {"task_id": task_id, "status": "ERROR", "error_message": f"并发执行异常: {exc}", "knowledge_units": []}

            # Phase 2.5: Smart Repartitioning for failed tasks
            tasks_to_repartition = [task for task in execution_plan if results_map.get(task.get('task_id'), {}).get('status') == 'INCOMPLETE_OUTPUT_MAX_TOKENS']
            if tasks_to_repartition:
                logging.info(f"🔀 发现 {len(tasks_to_repartition)} 个需要智能重分割的任务")
                for task_to_split in tasks_to_repartition:
                    original_task_id = task_to_split.get('task_id')
                    sub_task_defs = self.planner_repartitioner.repartition(task_to_split)
                    if not sub_task_defs:
                        results_map[original_task_id] = {"task_id": original_task_id, "status": "ERROR", "error_message": "智能重分割失败", "knowledge_units": []}
                        continue
                    
                    # 创建新的子任务
                    new_sub_tasks = []
                    for sub_def in sub_task_defs:
                        sub_task = json.loads(json.dumps(task_to_split))
                        sub_task.update(sub_def)
                        sub_task["physical_pdf_slice_path"] = None # 清除旧的切片路径
                        new_sub_tasks.append(sub_task)

                    # 为新的子任务创建物理切片
                    new_sub_tasks = self.pdf_slicer.slice_and_update_plan(new_sub_tasks, pdf_full_path, output_dirs['slices'])
                    
                    # 执行子任务
                    sub_task_results = []
                    for sub_task in new_sub_tasks:
                        sub_task_results.append(self.extractor.extract(sub_task))

                    # 合并子任务结果
                    all_knowledge_units = []
                    is_fully_successful = all(res['status'] == 'SUCCESS' for res in sub_task_results)
                    if is_fully_successful:
                        for res in sub_task_results:
                            all_knowledge_units.extend(res.get('knowledge_units', []))
                        logging.info(f"✅ 成功处理和合并子任务 {original_task_id}")
                        results_map[original_task_id] = {"task_id": original_task_id, "status": "SUCCESS", "error_message": None, "knowledge_units": all_knowledge_units}
                    else:
                        logging.error(f"❌ 子任务执行失败 {original_task_id}")
                        results_map[original_task_id] = {"task_id": original_task_id, "status": "ERROR", "error_message": "子任务执行失败", "knowledge_units": []}

            # 生成最终结果
            ordered_results = [results_map[task.get('task_id')] for task in execution_plan if task.get('task_id') in results_map]
            failed_count = sum(1 for res in ordered_results if res["status"] != "SUCCESS")
            success_count = len(ordered_results) - failed_count
            
            # 保存结果
            pdf_rel_dir = Path(pdf_rel_path).parent
            output_filename = Path(pdf_rel_path).stem + ".jsonl"
            output_path = Path(output_dirs['jsonl']) / pdf_rel_dir / output_filename
            merge_results(ordered_results, output_path)
            
            if failed_count == 0:
                logging.info(f"🎉 === 成功完成处理: {pdf_rel_path} ({success_count}/{len(ordered_results)} 任务成功) ===")
                state_manager.mark_as_completed(pdf_rel_path, completed_pdfs, retry_pdfs)
            else:
                error_msg = f"部分任务失败: {success_count}/{len(ordered_results)} 任务成功, {failed_count} 任务失败"
                logging.warning(f"⚠️  === 部分完成处理: {pdf_rel_path} ({error_msg}) ===")
                state_manager.mark_as_failed(pdf_rel_path, error_msg, retry_pdfs)
                
        except Exception as e:
            error_msg = f"处理过程中发生严重错误: {str(e)}"
            logging.error(f"❌ {error_msg}", exc_info=True)
            state_manager.mark_as_failed(pdf_rel_path, error_msg, retry_pdfs)

    def process_batch(self, pdf_directory: str):
        """批量处理PDF目录"""
        
        # 保存原始PDF目录引用
        self.original_pdf_directory = pdf_directory
        
        logging.info(f"🚀 === 开始批量处理PDF目录: {pdf_directory} ===")
        
        # 1. 扫描目录结构
        scanner = DirectoryScanner()
        pdf_structure = scanner.scan_pdf_structure(pdf_directory)
        all_pdfs = scanner.get_all_pdf_relative_paths(pdf_structure)
        
        if not all_pdfs:
            logging.error(f"❌ 在目录 '{pdf_directory}' 中未找到任何PDF文件")
            return
        
        # 2. 创建输出目录结构
        dir_manager = DirectoryManager(pdf_directory)
        output_dirs = dir_manager.create_output_structure()
        
        # 创建镜像目录结构
        dir_manager.mirror_directory_structure(pdf_structure, output_dirs['jsonl'])
        dir_manager.mirror_directory_structure(pdf_structure, output_dirs['slices'])
        
        # 确保log目录存在
        Path(output_dirs['logs']).mkdir(exist_ok=True)
        
        logging.info(f"📂 输出目录配置:")
        logging.info(f"   JSONL输出: {output_dirs['jsonl']}")
        logging.info(f"   切片临时: {output_dirs['slices']}")
        logging.info(f"   日志目录: {output_dirs['logs']}")
        
        # 3. 初始化状态管理器
        state_manager = ProcessingStateManager(pdf_directory)
        completed_pdfs = state_manager.load_completed_pdfs()
        retry_pdfs = state_manager.load_retry_pdfs()
        
        # 4. 获取需要处理的PDF列表
        pending_pdfs = state_manager.get_pending_pdfs(all_pdfs)
        
        if not pending_pdfs:
            logging.info("🎉 所有PDF已完美处理完成！")
            return
        
        # 5. 批量处理PDF
        logging.info(f"🏃‍♂️ 开始处理 {len(pending_pdfs)} 个待处理PDF...")
        
        completed_count = len(completed_pdfs)
        total_count = len(all_pdfs)
        
        for pdf_rel_path in pending_pdfs:
            pdf_full_path = os.path.join(pdf_directory, pdf_rel_path)
            
            if not os.path.exists(pdf_full_path):
                logging.warning(f"⚠️  文件不存在，跳过: {pdf_full_path}")
                continue
            
            self.process_single_pdf(
                pdf_rel_path, pdf_full_path, output_dirs, 
                state_manager, completed_pdfs, retry_pdfs,
                total_count, completed_count
            )
            
            completed_count += 1
            logging.info("\n" + "="*80 + "\n")
        
        # 6. 最终统计
        final_completed = state_manager.load_completed_pdfs()
        final_retry = state_manager.load_retry_pdfs()
        
        logging.info(f"🎯 === 批量处理完成 ===")
        logging.info(f"   总PDF数量: {len(all_pdfs)}")
        logging.info(f"   已完美处理: {len(final_completed)}")
        logging.info(f"   需要重试: {len(final_retry)}")
        logging.info(f"   成功率: {len(final_completed)/len(all_pdfs)*100:.1f}%")


if __name__ == '__main__':
    # 配置详细的日志系统
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    
    # 确保log目录存在
    Path("log").mkdir(exist_ok=True)
    
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    
    # 清除现有处理器
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
    
    # 文件日志处理器 - 详细信息
    file_handler = logging.FileHandler(f'log/agent_run_{timestamp}.log', mode='w', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(threadName)s] - %(filename)s:%(lineno)d - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # 控制台日志处理器 - 简化信息
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # 主程序执行
    pdf_directory = "国家级法律条款"
    
    if not os.path.exists(pdf_directory):
        logging.error(f"❌ 指定的PDF目录不存在: {pdf_directory}")
    else:
        agent = PDFRAGAgentV2()
        agent.process_batch(pdf_directory)